"""MLX text inference in an isolated environment, loading only local model files."""

import json
import os
import re
import sys
import time
import traceback
from difflib import SequenceMatcher
from pathlib import Path

from .control import activity
from .enrichment import (
    ANALYZE,
    GENERATION,
    POLISH,
    REVIEW,
    SYSTEM,
    chunks_for,
    ground_analysis,
    parse_model_json,
    polish_chunks_for,
    source_texts,
    validate_analysis,
    validate_polish,
)
from .files import atomic_json


def process(request):
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        TOKENIZERS_PARALLELISM="false",
    )
    work = Path(request["work"])
    units = request["units"]
    chunks = chunks_for(units, request["settings"]["chunk_chars"])
    polish_chunks = polish_chunks_for(units, request["settings"]["chunk_chars"])
    started = time.monotonic()
    metrics = []
    pending_reviews = []
    prompts = request.get(
        "prompts", {"system": SYSTEM, "polish": POLISH, "analyze": ANALYZE, "review": REVIEW}
    )
    sampling = request.get("generation", GENERATION)
    progress = {"stage": "加载文本模型"}

    def report(stage=None, **values):
        if stage:
            progress.clear()
            progress["stage"] = stage
        progress.update(values)
        activity(request.get("progress_path"), **progress)

    report()
    model = tokenizer = None
    if units:
        import mlx.core as mx
        from mlx_lm import load

        mx.set_cache_limit(256 * 1024 * 1024)
        mx.set_memory_limit(10 * 1024**3)
        model, tokenizer = load(request["model_dir"], tokenizer_config={"trust_remote_code": False})
        print("本地文本模型加载完成", flush=True)

    def generate_json(name, instruction, payload, validator, plain=False):
        path = work / f"{name}.json"
        if path.is_file():
            try:
                cached = json.loads(path.read_text())
                validator(cached)
                return cached
            except (ValueError, KeyError, TypeError):
                path.rename(work / f"{name}.invalid-{time.time_ns()}.json")
        if not plain:
            for rejected in sorted(work.glob(f"{name}.rejected-*.json"), reverse=True):
                try:
                    previous = json.loads(rejected.read_text())
                    if "达到长度上限" in previous["error"]:
                        continue
                    recovered, _ = parse_model_json(previous["response"])
                    validator(recovered)
                    atomic_json(path, recovered)
                    return recovered
                except (ValueError, KeyError, TypeError):
                    continue
        import mlx.core as mx
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_logits_processors, make_sampler

        error = ""
        for attempt in range(2):
            content = (
                instruction
                + "\n"
                + (
                    "\n\n".join(u["text"] for u in payload)
                    if plain
                    else json.dumps(payload, ensure_ascii=False)
                )
            )
            if error:
                content += (
                    "\n上次输出未通过检查，请修复并重新输出完整"
                    + ("正文" if plain else "JSON")
                    + "。错误："
                    + error
                )
            prompt = tokenizer.apply_chat_template(
                [
                    {
                        "role": "system",
                        "content": prompts["system"].replace(
                            "只输出一个合法JSON对象，不输出Markdown围栏、思考过程或解释。",
                            "只输出润色后的正文，不输出解释、标题或JSON。",
                        )
                        if plain
                        else prompts["system"],
                    },
                    {"role": "user", "content": content},
                ],
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=(not plain and sampling.get("analysis_thinking", False)),
            )
            tokens = tokenizer.encode(prompt)
            if len(tokens) > request["settings"]["max_input_tokens"]:
                raise ValueError("输入超过本机文本处理预算；请减小 analysis.chunk_chars 后重试")

            def infer(input_tokens, budget, temperature, top_p, label):
                pieces, last = [], None
                for last in stream_generate(
                    model,
                    tokenizer,
                    input_tokens,
                    max_tokens=budget,
                    sampler=make_sampler(temp=temperature, top_p=top_p, top_k=sampling["top_k"]),
                    logits_processors=make_logits_processors(),
                    prefill_step_size=sampling["prefill_step_size"],
                ):
                    pieces.append(last.text)
                    if last.generation_tokens % 256 == 0:
                        report(tokens=last.generation_tokens)
                        print(f"{name} {label}已生成 {last.generation_tokens} tokens", flush=True)
                return last, "".join(pieces)

            thinking = not plain and sampling.get("analysis_thinking", False)
            mx.random.seed(sampling["seed"] + attempt)
            response, text = infer(
                tokens,
                min(
                    request["settings"]["max_output_tokens"],
                    sampling.get("thinking_budget_tokens", 1536),
                )
                if thinking
                else request["settings"]["max_output_tokens"],
                sampling.get("thinking_temperature", sampling["temperature"])
                if thinking
                else sampling["temperature"],
                sampling.get("thinking_top_p", sampling["top_p"])
                if thinking
                else sampling["top_p"],
                "推理" if thinking else "正文",
            )
            thinking_tokens = response.generation_tokens if response is not None and thinking else 0
            if thinking and response is not None and response.finish_reason == "length":
                prefix = text if "</think>" in text else text + "\n</think>\n\n"
                continuation = tokenizer.encode(prompt + prefix)
                if len(continuation) > request["settings"]["max_input_tokens"]:
                    raise ValueError("推理续写超过本机上下文预算，请减小 chunk_chars")
                response, final = infer(
                    continuation,
                    request["settings"]["max_output_tokens"],
                    sampling["temperature"],
                    sampling["top_p"],
                    "成稿",
                )
                text = prefix + final
            text = text.strip()
            if thinking and "</think>" in text:
                text = text.split("</think>", 1)[1].strip()
            try:
                if response is None or response.finish_reason != "stop":
                    raise ValueError("模型输出达到长度上限，不交付截断内容")
                if plain:
                    result, repaired_keys = polish_result(text, payload), 0
                else:
                    result, repaired_keys = parse_model_json(text)
                validator(result)
                atomic_json(path, result)
                metrics.append(
                    {
                        "stage": name,
                        "prompt_tokens": response.prompt_tokens,
                        "generation_tokens": response.generation_tokens,
                        "tokens_per_second": response.generation_tps,
                        "peak_memory_gb": response.peak_memory,
                        "json_key_quote_repairs": repaired_keys,
                        "thinking_tokens": thinking_tokens,
                    }
                )
                print(
                    f"已完成 {name}，输出 {response.generation_tokens} tokens，"
                    f"{response.generation_tps:.1f} tokens/s，"
                    f"峰值 {response.peak_memory:.2f} GB",
                    flush=True,
                )
                mx.clear_cache()
                return result
            except (ValueError, KeyError, TypeError) as exc:
                error = str(exc)
                # Private generated content stays in the recording's local work directory.
                atomic_json(
                    work / f"{name}.rejected-{attempt}.json", {"error": error, "response": text}
                )
                print(f"{name} 校验未通过，将修复：{error}", flush=True)
                mx.clear_cache()
        raise ValueError(f"{name} 两次生成仍未通过检查：{error}")

    def polish_result(text, chunk):
        ids = [u["id"] for u in chunk]
        original = "".join(u["text"] for u in chunk)
        changes = []
        for tag, a, b, c, d in SequenceMatcher(None, original, text, autojunk=False).get_opcodes():
            if (
                tag != "replace"
                or not any(ch.isalnum() for ch in original[a:b])
                or not any(ch.isalnum() for ch in text[c:d])
            ):
                continue
            old = original[max(0, a - 1) : min(len(original), b + 1)]
            new = text[max(0, c - 1) : min(len(text), d + 1)]
            changes.append(
                {
                    "unit_ids": ids,
                    "original": old,
                    "revised": new,
                    "reason": "字词与语句调整（自动对比，请核对原文）",
                }
            )
            if len(changes) >= 6:
                break
        doubts = [
            {"unit_ids": ids, "text": m.group(0)}
            for m in re.finditer(r"[^。！？\n]{0,35}〔待核对〕", text)
        ]
        return {
            "paragraphs": [{"unit_ids": ids, "text": text}],
            "corrections": changes,
            "uncertainties": doubts,
        }

    def write_title():
        """A short name from the transcript; plain text, no thinking, validated and cached."""
        from .titles import TITLE_SYSTEM, clean_title, title_source

        path = work / "title.json"
        if path.is_file():
            try:
                return clean_title(json.loads(path.read_text())["title"])
            except (ValueError, KeyError, TypeError):
                path.rename(work / f"title.invalid-{time.time_ns()}.json")
        import mlx.core as mx
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler

        report("生成录音名称")
        source, error = title_source(units), ""
        for attempt in range(3):
            content = prompts["title"] + "\n" + source
            if error:
                content += f"\n上次输出不符合要求：{error}。请只输出一句符合要求的名称。"
            prompt = tokenizer.apply_chat_template(
                [{"role": "system", "content": TITLE_SYSTEM}, {"role": "user", "content": content}],
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            tokens = tokenizer.encode(prompt)
            if len(tokens) > request["settings"]["max_input_tokens"]:
                raise ValueError("命名输入超过本机文本处理预算")
            mx.random.seed(sampling["seed"] + attempt)
            pieces = []
            for response in stream_generate(
                model,
                tokenizer,
                tokens,
                max_tokens=80,
                sampler=make_sampler(
                    temp=sampling["temperature"], top_p=sampling["top_p"], top_k=sampling["top_k"]
                ),
                prefill_step_size=sampling["prefill_step_size"],
            ):
                pieces.append(response.text)
            text = "".join(pieces).strip()
            if "</think>" in text:
                text = text.split("</think>", 1)[1]
            try:
                title = clean_title(text)
            except ValueError as exc:
                error = str(exc)
                print(f"录音名称未通过检查，将重试：{error}", flush=True)
                continue
            atomic_json(path, {"title": title})
            mx.clear_cache()
            print(f"已生成录音名称：{title}", flush=True)
            return title
        raise ValueError(f"录音名称三次生成仍未通过检查：{error}")

    if request.get("mode") == "retrieval":
        from .retrieval import validate_retrieval
        result = generate_json('answer', request['instruction'],
            {'question': request['question'], 'sources': [{'source': i, 'text': s['text']} for i, s in enumerate(request['sources'])]},
            lambda r: validate_retrieval(r, request['retrieval_mode'], request['sources']))
        atomic_json(work / 'answer.json', result)
        return

    if request.get("mode") == "title":
        if units:
            write_title()
        return

    sources = source_texts(units)

    def analyze_checked(name, basis, allowed):
        instruction = prompts["analyze"]
        if basis and "brief" in basis[0]:
            instruction += "\n以下为覆盖全部录音的分段分析，请合并重复主题、保留分歧和明确行动，沿用source_ids。"
        draft = generate_json(name, instruction, basis, lambda r: validate_analysis(r, allowed))
        return ground_analysis(draft, sources)

    polished, summaries = [], []
    for i, chunk in enumerate(polish_chunks):
        reusable = request.get('reuse_polished')
        if isinstance(reusable, list) and len(reusable) == len(polish_chunks):
            validate_polish(reusable[i], chunk)
            polished.append(reusable[i])
            continue
        report("整理文稿", completed=i, total=len(polish_chunks), unit="段")
        polished.append(
            generate_json(
                f"polish-{i}",
                prompts["polish"],
                chunk,
                lambda r, c=chunk: validate_polish(r, c),
                plain=True,
            )
        )
    for i, chunk in enumerate(chunks):
        report("分析原文", completed=i, total=len(chunks), unit="段")
        source = [{k: v for k, v in u.items() if k != "id"} for u in chunk]
        allowed = {u["segment"] for u in chunk}
        summaries.append(analyze_checked(f"analysis-{i}", source, allowed))
    allowed = {u["segment"] for u in units}
    level = 0
    while len(summaries) > 1:
        report("汇总长文分析", level=level + 1)
        if level >= 10:
            raise ValueError("长文分析无法在预算内完成汇总，保留分段进度")
        groups, current, size = [], [], 0
        for summary in summaries:
            n = len(json.dumps(summary, ensure_ascii=False))
            if current and size + n > 14000:
                groups.append(current)
                current, size = [], 0
            current.append(summary)
            size += n
        if current:
            groups.append(current)
        if len(groups) == len(summaries):
            raise ValueError("分段分析过长，未丢弃任何内容；请调低文本分段大小后重试")
        summaries = [
            analyze_checked(f"merge-{level}-{i}", group, allowed) for i, group in enumerate(groups)
        ]
        level += 1
    atomic_json(
        work / "result.json",
        {
            "fingerprint": request["fingerprint"],
            "units": units,
            "polished": polished,
            "analysis": summaries[0] if summaries else None,
            "review_issues": pending_reviews,
            "metrics": {"elapsed_seconds": time.monotonic() - started, "stages": metrics},
        },
    )
    print("转写交付稿与分析材料生成完成", flush=True)
    if units and prompts.get("title"):
        # A naming failure never discards the delivered documents; it is retried later.
        try:
            write_title()
        except Exception as exc:
            print(f"录音名称生成未完成，原名称保留：{exc}", flush=True)


def main():
    try:
        process(json.loads(Path(sys.argv[1]).read_text()))
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
