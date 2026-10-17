"use strict";
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const escapeHTML = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icons = {
 library:'<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 7h8M8 11h8M8 15h5"/>',
 star:'<path d="m12 3 2.8 5.8 6.4.9-4.6 4.5 1.1 6.3L12 17.5l-5.7 3 1.1-6.3L2.8 9.7l6.4-.9Z"/>',
 queue:'<path d="M9 5h11M9 12h11M9 19h11M3 5h1M3 12h1M3 19h1"/>',
 alert:'<path d="M10.2 4.6 2.6 18a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.8 4.6a2 2 0 0 0-3.6 0Z"/><path d="M12 9v4m0 4h.01"/>',
 archive:'<rect x="3" y="3" width="18" height="4" rx="1"/><path d="M5 7v13h14V7M9 11h6"/>',
 settings:'<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
 refresh:'<path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2"/>',
 search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
 wave:'<path d="M4 10v4M8 5v14M12 2v20M16 6v12M20 10v4"/>',
 arrow:'<path d="m9 5 7 7-7 7"/>',
 back:'<path d="m14 6-6 6 6 6M8 12h13"/>',
 check:'<path d="m5 12 4 4L19 6"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 download:'<path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5"/>',
 trash:'<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
 agent:'<rect x="4" y="5" width="16" height="13" rx="2"/><path d="m8 10 2 2-2 2M12 14h4"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.library}</svg>`;
$$('[data-icon]').forEach(el => el.innerHTML = icon(el.dataset.icon));
const names = {library:'资料库',favorites:'我的收藏',queue:'处理队列',attention:'需要处理',archive:'已归档',settings:'设置与运行环境',search:'搜索与问答',tasks:'待办',people:'人物',terms:'个人词库'};
const state = {data:null,view:'library',filter:'all',query:'',sort:'newest',selected:new Set(),detail:null,tab:'smart',dirty:false,docText:'',renderSerial:0,uploading:false,online:false};
let toastTimer, pollBusy = false;
const timecode = value => {const n = Math.max(0, Math.floor(Number(value) || 0)); return (n >= 3600 ? `${Math.floor(n/3600)}:` : '') + `${Math.floor(n/60)%60}`.padStart(2,'0') + ':' + `${n%60}`.padStart(2,'0');};
const duration = n => n == null ? '时长待识别' : n < 60 ? `${Math.round(n)} 秒` : `${Math.floor(n/60)} 分 ${Math.round(n%60)} 秒`;
const date = v => {if (!v) return '未知'; const d = new Date(typeof v === 'number' ? v*1000 : v); return Number.isNaN(+d) ? '未知' : d.toLocaleDateString('zh-CN', {year:'numeric',month:'2-digit',day:'2-digit'});};
const relative = v => {if (!v) return '尚无记录'; const seconds = Math.max(0, Date.now()/1000 - (typeof v === 'number' ? v : Date.parse(v)/1000)); return seconds<60 ? '刚刚' : seconds<3600 ? `${Math.floor(seconds/60)} 分钟前` : seconds<86400 ? `${Math.floor(seconds/3600)} 小时前` : date(v);};
const isActive = i => i.asr_status === 'running' || i.text_status === 'running' || i.speaker_status === 'running';
const isIssue = i => ['failed','interrupted','missing'].includes(i.asr_status) || ['failed','interrupted'].includes(i.text_status) || ['failed','interrupted','blocked'].includes(i.speaker_status) || i.needs_review;
const hasOriginal = i => i.available.includes('transcript.simplified.md') || i.available.includes('transcript.md');
const pending = () => state.data.requests.filter(r => ['queued','running'].includes(r.status));
const scanning = () => pending().some(r => r.kind === 'scan' && (r.status==='running'||!r.automatic));
const scanButton = (label='立即扫描') => `<button data-action="scan" data-label="${label}"${scanning()?' disabled':''}>${scanning()?'扫描中…':label}</button>`;
function announceScans(previous) {
 for (const r of state.data.requests) {
  if (r.kind !== 'scan' || !previous.has(r.id) || !['completed','failed','interrupted'].includes(r.status)) continue;
  toast(r.status === 'completed' ? `扫描完成：${r.summary || '没有新录音'}` : `扫描未完成：${r.error || '请查看运行记录'}`, r.status !== 'completed');
 }
}
function toast(text, error=false) {const el=$('#toast');el.textContent=text;el.className=`toast${error?' error':''}`;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,error?8000:4500);}
async function api(path, data) {const response = await fetch(path, {credentials:'same-origin', ...(data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})});let result;try{result=await response.json();}catch{throw new Error('服务返回异常，请检查工作台是否仍在运行。');}if(!response.ok) throw new Error(result.error || '操作未完成');return result;}
async function act(action, extra={}) {return api('/api/action', {action,...extra});}
function badge(label, type='') {return `<span class="badge ${type}">${escapeHTML(label)}</span>`;}
function status(i) {
 if(i.asr_status==='running') return badge('转写中')+'<small class="stage-sub">'+escapeHTML(i.progress.stage || '正在识别音频')+'</small>';
 if(['failed','interrupted','missing'].includes(i.asr_status)) return badge(i.asr_status==='missing'?'原稿需要修复':i.asr_status==='interrupted'?'转写已中断':'转写失败','error');
 if(!hasOriginal(i)) return badge('等待转写','neutral');
 if(i.speaker_status==='running') return badge('区分说话人中')+'<small class="stage-sub">'+escapeHTML(i.progress.stage || '分析声音与发言归属')+'</small>';
 if(i.text_status==='running') return badge('原稿可读')+'<small class="stage-sub">'+escapeHTML(i.progress.stage || '文稿生成中')+'</small>';
 if(['failed','interrupted'].includes(i.text_status)) return badge('原稿可读')+'<small class="stage-sub">文稿待重试</small>';
 if(i.needs_review) return badge('文稿待核对','warning');
 if(i.text_speaker_stale) return badge('原稿已区分说话人')+'<small class="stage-sub">整理与分析的说话人信息待更新</small>';
 if(i.text_available) return badge('文稿已就绪');
 return badge('原稿可读')+`<small class="stage-sub">${i.text_status==='disabled'?'未启用文稿分析':'文稿等待生成'}</small>`;
}
function progressHTML(progress={}) {return `<div class="progress-wrap">${progress.total>0?`<progress value="${Math.min(progress.completed||0,progress.total)}" max="${progress.total}" aria-label="${escapeHTML(progress.stage)}进度"></progress><small>${progress.unit==='秒'?`${timecode(progress.completed)} / ${timecode(progress.total)} 音频`:`已完成 ${progress.completed||0} / ${progress.total} ${escapeHTML(progress.unit||'段')}`}${progress.tokens?` · 本段已生成 ${progress.tokens} tokens`:''}</small>`:''}<small>最近活动 ${relative(progress.updated)}</small></div>`;}
function modal(title, html) {$('#modal-title').textContent=title;$('#modal-body').innerHTML=html;if(!$('#modal').open)$('#modal').showModal();}
function empty(title, subtitle, action='') {return `<div class="empty">${icon('wave')}<h2>${title}</h2><p>${subtitle}</p>${action}</div>`;}
function heading(title, description, actions='') {return `<div class="page-heading"><div><div class="eyebrow">听见 · 留下 · 再发现</div><h1>${title}</h1><p>${description}</p></div><div class="heading-actions">${actions}</div></div>`;}
function updateShell() {
 const d=state.data, active=d.items.filter(isActive), issues=d.items.filter(isIssue), scanErrors=d.scan.errors?.length||0;
 $('#nav-library').textContent=d.items.filter(i=>!i.archived).length;
 $('#nav-queue').textContent=active.length + d.requests.filter(r=>r.status==='queued').length+(d.backlog?.length||0);
 $('#nav-attention').textContent=issues.length+scanErrors;
 $$('.nav-item[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===state.view));
 $('#breadcrumb').textContent=`工作空间 / ${state.view==='detail'?'资料库 / 录音详情':names[state.view]}`;
 const text=d.paused?(active.length?'将在当前处理结束后暂停':'后续处理已暂停'):active.length?`${active[0].progress.stage||'正在处理'} · ${active[0].title}`:d.busy?'模型正在被其他任务使用':pending().length?`${pending().length} 个请求等待处理`:'当前没有运行中的任务';
 $('#runtime').innerHTML=`<span class="status-dot"></span><span>${escapeHTML(text)}</span><div class="runtime-right">${d.legacy_service?.removed?'<span class="secondary-status">检测到旧自动服务，已停用</span>':''}<button data-action="pause">${d.paused?'恢复处理':'暂停后续'}</button></div>`;
 $$('[data-action="scan"][data-label]').forEach(b=>{b.disabled=scanning();b.textContent=scanning()?'扫描中…':b.dataset.label;});
 $('#connection').textContent='本地已连接';$('#connection').classList.remove('offline');
}
async function refresh(force=false) {
 if(pollBusy)return;pollBusy=true;
 try {
  const previous=new Set((state.data?.requests||[]).filter(r=>r.kind==='scan'&&['queued','running'].includes(r.status)).map(r=>r.id));
  state.data=await api('/api/library');state.online=true;updateShell();announceScans(previous);
  if(!state.detail&&['search','tasks','people','terms','settings'].includes(state.view)){if(force)renderMain();}
  else if(!state.detail){if(force||!$('#library-table')&& !['queue','attention','settings'].includes(state.view))renderMain();else if(['library','favorites','archive'].includes(state.view))renderRows();else if(!$('#modal').open)renderMain();}
  else {
   const current=state.data.items.find(i=>i.id===state.detail.id);
   if(current){
    const changed=JSON.stringify(current.available)!==JSON.stringify(state.detail.available)||current.text_generated_at!==state.detail.text_generated_at||current.text_status!==state.detail.text_status;
    Object.assign(state.detail,current);
    $('#detail-status').innerHTML=detailStatus(current);
    $('#detail-progress').innerHTML=isActive(current)?`<div class="activity-banner"><div class="activity-copy"><strong>${escapeHTML(current.progress.stage||'正在处理')}</strong>${progressHTML(current.progress)}${estimatesHTML(current)}</div></div>`:'';
    if(changed&&!state.dirty&&state.tab!=='notes'){state.detail=await api(`/api/detail?id=${current.id}`);await renderDocument();}
   }
  }
 } catch(error){state.online=false;$('#connection').textContent='连接已断开';$('#connection').classList.add('offline');if(!state.data)$('#main').innerHTML=empty('暂时无法连接工作台',escapeHTML(error.message),'<button data-action="refresh">重新连接</button>');if(force)toast(error.message,true);}
 finally {pollBusy=false;}
}
function canLeave(){return !state.dirty || confirm('笔记尚未保存。离开会丢失当前修改，确定离开吗？');}
function navigate(view) {
 if(!canLeave())return;
 state.dirty=false;state.detail=null;state.view=view;state.query='';state.filter='all';state.selected.clear();
 history.replaceState(null,'',`#${view}`);updateShell();renderMain();window.scrollTo(0,0);
}
function renderMain() {
 if(!state.data)return;
 if(['search','tasks','people','terms'].includes(state.view))return renderKnowledgePage().catch(error=>toast(error.message,true));
 if(state.view==='queue')return renderQueue();
 if(state.view==='attention')return renderAttention();
 if(state.view==='settings')return renderSettings();
 renderLibrary();
}
function activityBanner() {
 const active=state.data.items.find(isActive), waiting=pending();
 if(active)return `<div class="activity-banner">${icon('wave')}<div class="activity-copy"><strong>${escapeHTML(active.progress.stage||'正在处理')} · ${escapeHTML(active.title)}</strong><small>${hasOriginal(active)?'原始转写已保存，可以先开始阅读。':'本机处理进行中，完成的文稿将自动出现在资料库。'}</small></div><button data-open="${active.id}">查看进度 →</button></div>`;
 if(state.data.paused)return `<div class="activity-banner">${icon('clock')}<div class="activity-copy"><strong>后续任务已暂停</strong><small>已完成的文稿可以继续阅读；恢复后继续处理等待中的任务。</small></div><button data-action="pause">恢复处理</button></div>`;
 if(waiting.length)return `<div class="activity-banner">${icon('queue')}<div class="activity-copy"><strong>${waiting.length} 个操作请求等待处理</strong><small>与后台任务共用本地模型，当前任务结束后继续。</small></div><button data-view="queue">查看队列 →</button></div>`;
 return '';
}
function renderLibrary() {
 const d=state.data, items=d.items.filter(i=>!i.archived);
 $('#main').innerHTML=heading(names[state.view],state.view==='favorites'?'把值得反复阅读的声音，留在手边。':state.view==='archive'?'已归档的资料仍完整保留，可随时恢复。':'让每一段声音，成为可以阅读和使用的资料。',scanButton())+
 (state.view==='library'?`<div class="count-strip"><button class="metric" data-filter="all"><strong>${items.length.toString().padStart(2,'0')}</strong><span>全部录音</span></button><button class="metric" data-filter="ready"><strong>${items.filter(hasOriginal).length.toString().padStart(2,'0')}</strong><span>可阅读</span></button><button class="metric" data-filter="working"><strong>${items.filter(isActive).length.toString().padStart(2,'0')}</strong><span>正在处理</span></button><button class="metric" data-view="attention"><strong>${items.filter(isIssue).length.toString().padStart(2,'0')}</strong><span>需要关注</span></button></div>`:'')+
 activityBanner()+`<div class="toolbar"><label class="search-box">${icon('search')}<input id="library-search" type="search" placeholder="搜索录音名称或原文预览…" aria-label="搜索录音名称或原文预览" value="${escapeHTML(state.query)}"></label><select id="sort" aria-label="排序"><option value="newest">最近加入</option><option value="recorded">录制时间</option><option value="title">名称顺序</option></select></div>
 <div class="list-toolbar"><div class="chips">${[['all','全部'],['ready','可阅读'],['working','处理中'],['issues','待处理']].map(([k,v])=>`<button class="chip ${state.filter===k?'active':''}" data-filter="${k}">${v}</button>`).join('')}</div><span id="result-count"></span></div><div id="batch"></div>
 <div id="library-table"></div><div class="table-footer"><span>可将音频拖入窗口，相同内容自动去重</span><span>原始音频与完整转写始终保留</span></div>`;
 $('#sort').value=state.sort;renderRows();
}
function filteredItems() {
 let list=state.data.items.filter(i=>state.view==='archive'?i.archived:!i.archived);
 if(state.view==='favorites')list=list.filter(i=>i.favorite);
 if(state.filter==='ready')list=list.filter(hasOriginal);
 if(state.filter==='working')list=list.filter(isActive);
 if(state.filter==='issues')list=list.filter(isIssue);
 const query=state.query.trim().toLocaleLowerCase();
 if(query)list=list.filter(i=>`${i.title} ${i.preview}`.toLocaleLowerCase().includes(query));
 return list.sort((a,b)=>state.sort==='title'?a.title.localeCompare(b.title,'zh-CN'):state.sort==='recorded'?(Date.parse(b.recorded_at)||0)-(Date.parse(a.recorded_at)||0):b.created-a.created);
}
function renderRows() {
 if(!$('#library-table'))return;
 const items=filteredItems();$('#result-count').textContent=`${items.length} 条录音`;
 $$('.chip[data-filter]').forEach(el=>el.classList.toggle('active',el.dataset.filter===state.filter));
 const selected=[...state.selected];
 $('#batch').innerHTML=selected.length?`<div class="batch-bar"><span>已选 ${selected.length} 条</span><button data-action="batch-export">导出原稿 TXT</button><button data-action="batch-archive">${state.view==='archive'?'恢复到资料库':'归档'}</button><button class="danger" data-action="batch-delete">删除</button><button class="text-button" data-action="clear-selection">取消选择</button></div>`:'';
 if(!items.length){$('#library-table').innerHTML=empty(state.query?'没有找到匹配的录音':state.filter!=='all'?'这里暂时没有录音':state.view==='favorites'?'值得重听的声音，从这里开始':state.view==='archive'?'还没有归档资料':'第一段声音，从这里开始',state.query?'试试其他名称或原文中的词语。':'导入一段录音，或点击「立即扫描」读取已同步到这台 Mac 的语音备忘录。',state.view==='library'?'<button class="primary" data-action="import">＋ 导入录音</button>':'');return;}
 $('#library-table').innerHTML=`<table class="record-table"><thead><tr><th class="check-col"><input type="checkbox" id="select-all" aria-label="选择当前列表全部录音" ${items.every(i=>state.selected.has(i.id))?'checked':''}></th><th>录音 / 内容预览</th><th class="time-col">时长</th><th class="status-col">处理状态</th><th class="more-col">操作</th></tr></thead><tbody>${items.map(i=>`<tr><td class="check-col"><input type="checkbox" data-select="${i.id}" aria-label="选择 ${escapeHTML(i.title)}" ${state.selected.has(i.id)?'checked':''}></td><td><div class="record-title"><span class="record-icon">${icon('wave')}</span><div class="record-text"><button class="record-name" data-open="${i.id}" title="${escapeHTML(i.title)}">${escapeHTML(i.title)}</button><div class="record-sub"><span>${i.source_kind}</span><span class="separator">·</span><span>${i.recorded_at?date(i.recorded_at)+' 录制':date(i.created)+' 加入'}</span>${i.versions.length>1?`<span>· ${i.versions.length} 个版本</span>`:''}</div>${i.preview?`<div class="preview">${escapeHTML(i.preview)}</div>`:''}</div></div></td><td class="time-col time-cell">${i.duration!=null?timecode(i.duration):'—'}<small>${i.duration!=null?'音频时长':'等待识别'}</small></td><td class="status-col">${status(i)}</td><td class="more-col"><button class="icon-button star-button ${i.favorite?'selected':''}" data-favorite="${i.id}" aria-label="${i.favorite?'取消收藏':'收藏'} ${escapeHTML(i.title)}">${icon('star')}</button><button class="icon-button delete-button" data-delete="${i.id}" aria-label="删除 ${escapeHTML(i.title)}" title="删除">${icon('trash')}</button></td></tr>`).join('')}</tbody></table>`;
}
function renderQueue(){
 const d=state.data,active=d.items.filter(isActive);
 const labels={queued:'等待执行',running:'正在执行',completed:'操作已完成',failed:'需要重试',interrupted:'运行已中断',cancelled:'已取消'};
 $('#main').innerHTML=heading('处理队列','看清当前进度，也知道下一步会发生什么。',`<button data-action="pause">${d.paused?'恢复处理':'暂停后续任务'}</button>`)+
 `<div class="notice">${d.paused?'后续任务已暂停。当前正在运行的阶段会继续完成。':'语音识别与文稿分析依次使用本地模型。新操作会在当前任务结束后执行。'} 调整优先级只影响尚未开始的工作台请求。</div>`+
 `<h2 class="section-title">当前模型任务</h2>${active.length?active.map(i=>`<div class="queue-row"><span class="record-icon">${icon('wave')}</span><div class="queue-copy"><h3>${escapeHTML(i.title)}</h3><p>${escapeHTML(i.progress.stage||'正在处理')} · ${hasOriginal(i)?'原稿已可阅读':'原稿生成中'}</p>${progressHTML(i.progress)}${estimatesHTML(i)}</div><button data-open="${i.id}">打开录音</button></div>`).join(''):`<p class="help-copy">${d.busy?'模型正在被其他进程使用。':'当前没有运行中的模型任务。'}</p>`}`+
 `${d.backlog?.length?`<h2 class="section-title">语音备忘录 · 本地待转写</h2>${d.backlog.map(i=>`<div class="queue-row"><span class="queue-number">${icon('clock')}</span><div class="queue-copy"><h3>${escapeHTML(i.name)}</h3><p>${i.settling?'等待文件保存或同步稳定':'等待扫描与处理'} · ${(i.size/1024/1024).toFixed(1)} MB</p>${estimatesHTML(i)}</div></div>`).join('')}`:''}`+
 `<h2 class="section-title">操作请求与最近记录</h2>${d.requests.length?d.requests.map((r,index)=>`<div class="queue-row"><span class="queue-number">${String(index+1).padStart(2,'0')}</span><div class="queue-copy"><h3>${escapeHTML(r.title)}</h3><p>${badge(labels[r.status]||r.status,['failed','interrupted'].includes(r.status)?'error':r.status==='queued'?'neutral':'')} · ${relative(r.created)}提交${r.priority?' · 优先':''}</p>${r.summary?`<p>${escapeHTML(r.summary)}</p>`:''}${r.error?`<p>${escapeHTML(r.error)}</p>`:''}${r.recordings?.map(i=>`<p>${escapeHTML(i.name)}</p>${estimatesHTML(i)}`).join('')||''}${r.estimates?estimatesHTML(r):''}${r.status==='queued'?`<p>${d.paused?'等待恢复处理':d.pipeline_busy||d.busy?'等待当前任务释放资源':r.automatic?escapeHTML(d.preferences?.automatic?d.automatic_status:'自动处理已关闭'):'等待调度'}</p>`:''}</div><div class="queue-actions">${r.status==='queued'?`<button data-request="${r.id}" data-operation="prioritize">优先处理</button><button data-request="${r.id}" data-operation="cancel">取消</button>`:''}${['failed','interrupted'].includes(r.status)?`<button data-request="${r.id}" data-operation="retry_request">重试操作</button>`:''}${r.status!=='queued'?`<button class="text-button" data-log-request="${r.id}">运行记录</button>`:''}</div></div>`).join(''):empty('没有等待中的操作','在这里查看导入、扫描和文稿生成的执行记录。')}`;
}
function renderAttention(){
 const items=state.data.items.filter(isIssue),errors=state.data.scan.errors||[];
 $('#main').innerHTML=heading('需要处理','运行问题与内容疑点，都有明确的下一步。')+
 errors.map(e=>`<div class="notice"><strong>${e.permission_denied?'无法读取语音备忘录目录':'语音备忘录导入异常'}</strong><p>${escapeHTML(e.error)}</p><p>已导入的录音仍可使用。${e.permission_denied?'请在系统设置的「完全磁盘访问权限」中允许实际运行的 Python，再重新扫描。':''}</p>${scanButton('重新扫描')}<button class="text-button" data-view="settings">查看运行环境</button></div>`).join('')+
 (items.length?items.map(i=>`<div class="queue-row"><span class="record-icon">${icon('alert')}</span><div class="queue-copy"><h3>${escapeHTML(i.title)}</h3><p>${hasOriginal(i)?'完整转写已保存，可以继续阅读。':'这条录音的转写尚未完成。'} ${i.needs_review?'生成文稿中有待核对项，请结合原文和录音检查。':''}</p>${i.text_error||i.asr_error||i.speaker_error?`<p>${escapeHTML(i.text_error||i.asr_error||i.speaker_error)}</p>`:''}</div><div class="queue-actions"><button data-open="${i.id}">打开核对</button>${!hasOriginal(i)?`<button data-retry-asr="${i.id}">重试转写</button>`:['failed','interrupted'].includes(i.text_status)?`<button data-enrich="${i.id}">仅重试文稿</button>`:''}</div></div>`).join(''):!errors.length?empty('暂时没有需要处理的问题','已生成的内容仍可随时对照原始录音核对。'):'');
}
function renderSettings(){
 const d=state.data,s=d.settings;
 $('#main').innerHTML=heading('设置与运行环境','了解本机的接收、处理与保存方式。')+preferencesHTML()+
 `<section class="settings-section"><h2>接收与运行</h2><p>自动处理按所选时机启动，也可随时立即扫描。关闭窗口后菜单栏继续监听新录音。</p><div class="setting-row"><span>手动扫描语音备忘录<small>Apple 负责同步，工作台只读取已经下载到 Mac 的录音。${d.scan.checked_at?`最近一次：发现 ${d.scan.found||0} 条 · 新录音 ${d.scan.copied||0} 条 · 等待同步 ${d.scan.waiting||0} 条`:'尚未扫描过'}</small></span><span class="setting-action"><strong>${relative(d.scan.checked_at)}</strong>${scanButton()}</span></div>${d.legacy_service?.removed?'<p class="help-copy">检测到旧的每 60 秒自动扫描服务，已停用并移除。</p>':''}<div class="setting-row"><span>后续任务<small>暂停会等待当前阶段完成，保留已经完成的结果。</small></span><button data-action="pause">${d.paused?'恢复处理':'暂停后续任务'}</button></div><div class="setting-row"><span>系统通知<small>通知权限由 macOS 系统设置控制。</small></span><button data-action="notify">发送测试通知</button></div></section>
 <section class="settings-section"><h2>本地模型</h2><p>日常处理不上传音频和文字。语音模型与文本模型依次使用本机资源。</p><div class="setting-row"><span>语音识别模型</span><strong>${escapeHTML(s.model)}</strong></div><div class="setting-row"><span>文稿优化与分析</span><strong>${s.analysis_enabled?'已启用 · Qwen3.5-9B':'未启用'}</strong></div><div class="setting-row"><span>区分说话人</span><strong>${s.speakers_enabled?'已启用 · 本地声音分析':'未启用'}</strong></div><div class="setting-row"><span>识别语言</span><strong>${({zh:'中文',en:'英文',auto:'自动识别'})[s.language]||escapeHTML(s.language)}</strong></div></section>
 <section class="settings-section"><h2>资料与个人内容</h2><p>个人笔记、收藏和归档状态独立保存，重新生成文稿不会覆盖它们。自动稿件请通过导出获得可自由编辑的副本。</p><div class="setting-row"><span>文稿保存位置</span><strong>${escapeHTML(s.output)}</strong></div><div class="setting-row"><span>高级配置<small>修改模型与识别参数后，已有文稿仍然保留。</small></span><strong>${escapeHTML(s.config)}</strong></div><p>请备份原始音频、output 和 state。通过工作台导入的本地音频副本保存在 state/workbench-audio，个人笔记保存在 state/workbench.sqlite3。</p></section>`;
}
function detailStatus(d){return `${badge(hasOriginal(d)?'原文可读':d.asr_status==='running'?'正在转写':'转写待处理',hasOriginal(d)?'':'neutral')}${badge(d.text_status==='running'?'文稿生成中':d.text_available?'智能稿可读':['failed','interrupted'].includes(d.text_status)?'文稿待重试':'文稿尚未生成', ['failed','interrupted'].includes(d.text_status)?'warning':d.text_available?'':'neutral')}${d.text_speaker_stale?badge('文稿说话人信息待更新','warning'):''}${d.speaker_status==='completed'?badge('已区分说话人'):d.speaker_status==='running'?badge('区分说话人中'):d.speaker_status==='blocked'?badge('说话人模型待准备','warning'):['failed','interrupted'].includes(d.speaker_status)?badge('说话人区分待重试','warning'):''}${d.reviewed||d.review?.complete?badge('已核对'):d.needs_review?badge('含待核对项','warning'):''}`;}
function speakersPanel(d){
 const list=d.speakers||[];if(!list.length)return '';
 return `<div class="info-divider"></div><h3>说话人</h3>${list.map(s=>`<div class="speaker-row"><span><strong>${escapeHTML(s.name||s.default)}</strong><small>${s.name?escapeHTML(s.default)+' · ':''}${s.segments} 段发言</small></span><button class="text-button" data-speaker="${escapeHTML(s.id)}">改名</button></div>`).join('')}<p class="help-copy">改一次即全文同步：原文、正文、分析稿与导出都会使用新名称。</p>`;
}
function speakerDialog(speaker){
 const s=(state.detail.speakers||[]).find(x=>x.id===speaker);if(!s)return;
 modal('修改说话人名称',`<p>为「${escapeHTML(s.default)}」设置名称，这条录音中该说话人的 ${s.segments} 段发言会全部同步更新。留空可恢复默认编号。</p><input id="speaker-input" maxlength="40" aria-label="说话人名称" placeholder="${escapeHTML(s.default)}" value="${escapeHTML(s.suggested?s.person_name:s.name)}"><label class="check-option"><input type="checkbox" id="remember-voice" ${s.suggested||s.person?'checked':''}>记住这个声音</label><label class="check-option"><input type="checkbox" id="speaker-me" ${s.me?'checked':''}>这是我</label><input type="hidden" id="speaker-id" value="${escapeHTML(s.id)}"><div class="actions"><button data-action="close-modal">取消</button><button class="primary" data-action="save-speaker">全部同步更新</button></div>`);
 $('#speaker-input').focus();
}
function deleteDialog(ids){
 const items=ids.map(id=>state.data.items.find(i=>i.id===id)||(state.detail?.id===id?state.detail:null)).filter(Boolean);
 if(!items.length)return;
 const busy=items.filter(isActive);
 if(busy.length)throw new Error(`${busy.length} 条录音正在处理，请等待当前阶段完成后再删除`);
 state.pendingDelete=items.map(i=>i.id);
 const apple=items.some(i=>i.source_kind==='语音备忘录');
 modal(items.length>1?`删除 ${items.length} 条录音`:'删除录音',`<p>${items.length>1?`将删除所选 ${items.length} 条录音`:`将删除「${escapeHTML(items[0].title)}」`}的原文、正文、分析稿、个人笔记和全部版本，<strong>此操作不可撤销</strong>。删除后再次扫描也不会重新转写这些录音。</p><label class="check-option"><input type="checkbox" id="delete-audio"><span>同时删除录音源文件<small>工作台导入或手动转写的原音频会移到「废纸篓」，可从废纸篓恢复。${apple?'语音备忘录 App 中的原录音不会被改动，需要时请在 App 内删除。':''}</small></span></label><div class="actions"><button data-action="close-modal">取消</button><button class="danger primary" data-action="confirm-delete">确认删除</button></div>`);
}
function agentDialog(){
 const d=state.detail,agents=state.data.settings.agents||{};
 const parts=[['transcript','完整转写（带时间戳与说话人）',hasOriginal(d)],['polished','正文',d.available.includes('polished.md')],['analysis','分析稿',d.available.includes('analysis.md')]].filter(p=>p[2]);
 modal('发送给 Agent',`<p>把这条录音整理成一份上下文文件，在终端启动 Agent 并让它先读完，接着就可以围绕录音提问。文件只保存在本机。</p><div class="agent-parts">${parts.map(([k,v],i)=>`<label class="check-option"><input type="checkbox" data-part="${k}" ${i===0?'checked':''}><span>${v}</span></label>`).join('')}</div><div class="agent-actions"><button class="primary" data-action="launch-agent" data-agent="claude">${icon('agent')} 在终端打开 Claude Code${agents.claude===false?'<small>未检测到 claude 命令</small>':''}</button><button class="primary" data-action="launch-agent" data-agent="codex">${icon('agent')} 在终端打开 Codex${agents.codex===false?'<small>未检测到 codex 命令</small>':''}</button></div><div class="actions"><button data-action="copy-agent-context">复制上下文</button><button data-action="download-agent-context">${icon('download')} 下载 .md</button></div><p class="help-copy">复制后可直接粘贴到 Claude、ChatGPT 或 Codex 应用的对话框中。</p>`);
}
function agentParts(){return $$('[data-part]',$('#modal')).filter(el=>el.checked).map(el=>el.dataset.part);}
async function openDetail(id, tab) {
 if(!canLeave())return;
 state.dirty=false;
 const d=await api(`/api/detail?id=${id}`);d.estimates=state.data.items.find(i=>i.id===id)?.estimates;act('seen_results').catch(()=>{});state.detail=d;state.view='detail';state.tab=tab||'smart';state.selected.clear();history.replaceState(null,'',`#recording=${id}`);updateShell();
 $('#main').innerHTML=`<button class="text-button back" data-view="library">${icon('back')} 返回资料库</button><div class="detail-heading"><div><div class="eyebrow">录音资料</div><h1>${escapeHTML(d.title)}</h1><p class="detail-meta">${d.source_kind} · ${duration(d.duration)} · ${d.recorded_at?date(d.recorded_at)+' 录制':'录制时间未知'} · ${date(d.created)} 加入${d.speakers?.length?' · '+d.speakers.map(s=>escapeHTML(s.name||s.default)).join('、'):''}</p></div><div class="detail-actions"><button data-action="agent">${icon('agent')} 发送给 Agent</button><button data-action="rename">重命名</button><button data-favorite="${d.id}">${d.favorite?'取消收藏':'收藏'}</button><button class="primary" data-action="export">导出</button></div></div><div class="detail-status" id="detail-status">${detailStatus(d)}</div><div id="detail-progress">${isActive(d)?`<div class="activity-banner"><div class="activity-copy"><strong>${escapeHTML(d.progress.stage||'正在处理')}</strong>${progressHTML(d.progress)}${estimatesHTML(d)}</div></div>`:''}</div>
 <div class="tabs" role="tablist" aria-label="录音内容">${[['smart','智能稿'],['original','原文'],['analysis','分析'],['notes','笔记'],['history','历史版本']].map(([k,v])=>`<button class="tab ${state.tab===k?'active':''}" role="tab" aria-selected="${state.tab===k}" data-tab="${k}">${v}</button>`).join('')}</div>
 <div class="detail-grid"><div><div id="reference"></div><section class="document" id="document" role="tabpanel"></section></div><aside class="document-info"><h3>资料信息</h3><div class="info-field"><small>来源</small>${d.source_kind}</div><div class="info-field"><small>语音识别</small>${escapeHTML(d.model||'等待识别')}</div><div class="info-field"><small>原始文件</small><div class="path">${escapeHTML(d.source)}</div></div><div class="info-field"><small>转写版本</small><select class="form-select" id="version-select" aria-label="选择转写版本">${d.versions.map((v,i)=>`<option value="${v.id}" ${v.id===d.id?'selected':''}>${date(v.created)} · ${i===0?'最新':v.id.slice(0,6)}</option>`).join('')}</select></div><div id="speakers-panel">${speakersPanel(d)}</div><div class="info-divider"></div><h3>处理操作</h3>${hasOriginal(d)?`<button data-enrich="${d.id}">补齐 / 重试文稿</button><button data-action="regenerate">重新生成文稿</button>`:`<button data-retry-asr="${d.id}">重试转写</button>`}${hasOriginal(d)&&state.data.settings.speakers_enabled?`<button data-diarize="${d.id}" ${d.speaker_status==='running'?'disabled':''}>补做 / 重试说话人区分</button>`:''}<button data-action="history">历史文稿</button><button data-action="refresh-detail">刷新内容</button><button data-action="reveal">在 Finder 中打开</button>${hasOriginal(d)&&state.data.settings.analysis_enabled?`<button data-action="regenerate-title">重新生成名称</button>`:''}<button data-action="archive">${d.archived?'恢复到资料库':'归档这条录音'}</button><button class="danger" data-delete="${d.id}">删除这条录音</button><div class="info-divider"></div><h3>运行记录</h3><button class="text-button" data-log-id="${d.id}">转写日志</button><button class="text-button" data-log-id="${d.id}" data-text="1">文稿日志</button><p class="help-copy">正文和分析由本地模型生成。点击文中时间引用，可对照原文并播放录音。</p></aside></div>
 <div class="audio-player">${icon('wave')}${d.audio_name?`<button class="jump" data-jump="-10" aria-label="后退 10 秒">−10s</button><audio id="audio" controls preload="metadata" src="/api/audio?id=${d.id}" aria-label="录音播放器"></audio><button class="jump" data-jump="10" aria-label="前进 10 秒">+10s</button><select id="speed" aria-label="播放速度"><option value="0.75">0.75×</option><option value="1" selected>1.0×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="2">2.0×</option></select>`:'<span class="audio-error">原音频已移动或不可读取，转写文稿仍可阅读。</span>'}</div><p id="audio-warning" class="help-copy"></p>`;
 if($('#audio')){
  $('#audio').addEventListener('timeupdate',()=>{const a=$('#audio');$$('.segment').forEach(el=>el.classList.toggle('current',+el.dataset.start<=a.currentTime&&+el.dataset.end>a.currentTime));});
  $('#audio').addEventListener('error',()=>{$('#audio-warning').innerHTML='浏览器无法直接播放此音频。<button class="text-button" data-action="compatible-audio">生成本地兼容音频</button> 原始录音保持不变。';});
 }
 await renderDocument();window.scrollTo(0,0);
}
function inlineMarkdown(text){
 const tokens=[];
 const keep=html=>{tokens.push(html);return `\uE000${tokens.length-1}\uE001`;};
 const restore=html=>html.replace(/\uE000(\d+)\uE001/g,(_,n)=>tokens[+n]||'');
 // Backslash escapes first: escaped < > * [ ] stay literal text, never markup or a stray "\".
 text=text.replace(/\\([\\`*_{}\[\]<>#!|~])/g,(_,c)=>keep(escapeHTML(c)));
 text=text.replace(/\[([^\]]+)\]\(([^)]+)\)/g,(_,label,url)=>{
  const shown=restore(escapeHTML(label));let out=shown;const reference=url.match(/^transcript(?:\.simplified)?\.md#(segment|turn)-(\d+)$/);
  if(reference)out=`<button class="ref-link" data-ref="${reference[2]}" data-ref-kind="${reference[1]}">${shown}</button>`;
  else if(['analysis.md','polished.md','notes.md','transcript.simplified.md','transcript.md'].includes(url))out=`<a href="#" data-tab="${url==='analysis.md'?'analysis':url==='polished.md'?'polished':'original'}">${shown}</a>`;
  else if(/^https?:\/\//.test(url))out=`<a href="${restore(escapeHTML(url))}" target="_blank" rel="noopener noreferrer">${shown}</a>`;
  return keep(out);
 });
 return restore(escapeHTML(text).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>'));
}
function markdown(text){
 text=text.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/,'');
 let out='',paragraph=[],list=false;
 const flush=()=>{if(paragraph.length){out+=`<p>${paragraph.join('<br>')}</p>`;paragraph=[];}if(list){out+='</ul>';list=false;}};
 for(const line of text.split('\n')){
  if(!line.trim()){flush();continue;}
  if(line.startsWith('[音频副本]')||line.startsWith('<a id='))continue;
  const h=line.match(/^(#{1,3})\s+(.+)/);
  if(h){flush();out+=`<h${h[1].length}>${inlineMarkdown(h[2])}</h${h[1].length}>`;}
  else if(line.startsWith('> ')){flush();out+=`<blockquote>${inlineMarkdown(line.slice(2))}</blockquote>`;}
  else if(line.startsWith('- ')){if(paragraph.length){out+=`<p>${paragraph.join('<br>')}</p>`;paragraph=[];}if(!list){out+='<ul>';list=true;}out+=`<li>${inlineMarkdown(line.slice(2))}</li>`;}
  else{if(list){out+='</ul>';list=false;}paragraph.push(inlineMarkdown(line));}
 }
 flush();return out;
}
async function renderDocument(){
 const d=state.detail,tab=state.tab,serial=++state.renderSerial;
 $$('.tab').forEach(el=>{el.classList.toggle('active',el.dataset.tab===tab);el.setAttribute('aria-selected',String(el.dataset.tab===tab));});
 $('#reference').innerHTML='';state.docText='';
 if(tab==='smart')return renderSmart();
 if(tab==='history')return renderRevisionHistory();
 if(tab==='notes'){
  $('#document').innerHTML=`<div class="document-bar"><span>独立保存 · 不受自动生成影响</span><span>⌘ S 保存</span></div><textarea id="notes-editor" class="notes-editor" aria-label="笔记" placeholder="写下你的理解、核对结果，或下一步行动…">${escapeHTML(d.notes)}</textarea><div class="notes-footer"><span id="save-state">${d.revision?'已保存 · 第 '+d.revision+' 版':'还没有笔记'}</span><button class="primary" data-action="save-notes">保存笔记</button></div>`;return;
 }
 if(tab==='original'){
  state.docText=d.segments.map(s=>(s.speaker_label?s.speaker_label+'：':'')+s.text).join('\n\n');
  $('#document').innerHTML=`<div class="document-bar"><label class="search-box">${icon('search')}<input type="search" id="segment-search" placeholder="在原文中查找…" aria-label="查找原文"></label><button class="text-button" data-action="copy">复制全文</button></div><button data-action="review-start">快速核对</button> <span>${d.review?.complete?'已核对':''}</span><div id="segments">${segmentsHTML(d.segments)}</div>`;return;
 }
 const name=tab==='polished'?'polished.md':'analysis.md';
 if(!d.available.includes(name)){
  $('#document').innerHTML=empty(tab==='polished'?'正文尚未就绪':'分析稿尚未就绪',d.text_status==='running'?'本地模型正在处理，你可以先阅读完整转写。':['failed','interrupted'].includes(d.text_status)?'这次文稿处理未完成，原始转写仍然保留。':'基于原始转写，生成适合阅读与使用的文稿。',`<button data-tab="original">阅读原文</button> ${d.text_status!=='running'?`<button class="primary" data-enrich="${d.id}">生成 / 重试文稿</button>`:''}`);return;
 }
 $('#document').innerHTML='<div class="initial-state"><span class="loader"></span><p>正在读取文稿…</p></div>';
 try {const doc=await api(`/api/document?id=${d.id}&name=${name}`);if(serial!==state.renderSerial)return;state.docText=doc.text.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/,'');$('#document').innerHTML=`<div class="document-bar"><span>${tab==='polished'?'优化口头表达 · 原文始终保留':'原话摘录与模型推断分别标注'}</span><button class="text-button" data-action="copy">复制文稿</button></div><article class="prose">${markdown(doc.text)}</article>`;}
 catch(error){if(serial===state.renderSerial)$('#document').innerHTML=empty('暂时无法读取文稿',escapeHTML(error.message),'<button data-action="refresh-detail">重新读取</button>');}
}
function segmentsHTML(segments,query=''){
 const q=query.trim().toLocaleLowerCase(),list=q?segments.filter(s=>s.text.toLocaleLowerCase().includes(q)):segments;
 if(!list.length)return empty(q?'没有匹配的原文':'尚无原文',q?'换一个词试试。':'识别完成后，这里会显示带时间戳的完整内容。');
 return list.map(s=>`<div class="segment ${s.review_reasons?.length&&!s.reviewed?'suspect':''}" id="segment-${s.id}" data-start="${s.start}" data-end="${s.end}"><button data-seek="${s.start}" aria-label="播放 ${timecode(s.start)}">${timecode(s.start)} ↗</button><p>${s.speaker_label?(s.speaker&&!s.overlap?`<button class="speaker-name" data-speaker="${escapeHTML(s.speaker)}" title="点击修改此说话人的名称，全文同步">${escapeHTML(s.speaker_label)}</button>`:`<strong>${escapeHTML(s.speaker_label)}</strong>`)+'<br>':''}<span class="segment-text" data-text-segment="${s.id}">${escapeHTML(s.text)}</span> <button class="text-button" data-edit-segment="${s.id}">修正</button>${s.review_reasons?.length?`<small>${escapeHTML(s.review_reasons.join(' · '))}${s.reviewed?' · 已核对':''}</small>`:''}</p></div>`).join('');
}
async function seek(seconds){const audio=$('#audio');if(!audio){toast('音频暂不可用，仍可核对原文。',true);return;}if(audio.readyState===0)await new Promise((resolve,reject)=>{audio.addEventListener('loadedmetadata',resolve,{once:true});audio.addEventListener('error',reject,{once:true});audio.load();});audio.currentTime=Math.max(0,seconds);try{await audio.play();}catch{toast('请点击播放器的播放按钮开始收听。');}}
async function saveNotes(){if(!state.detail||!$('#notes-editor'))return;const notes=$('#notes-editor').value;const result=await act('metadata',{id:state.detail.id,changes:{notes,revision:state.detail.revision}});state.detail.notes=notes;state.detail.revision=result.revision;state.dirty=false;$('#save-state').textContent='已保存 · 第 '+result.revision+' 版';toast('笔记已独立保存');}
const artifactLabels={'polished.md':'正文 · Markdown','polished.txt':'正文 · TXT','analysis.md':'分析稿 · Markdown','notes.md':'Markdown 笔记 · Markdown','transcript.simplified.txt':'完整文字（TXT）','transcript.simplified.md':'完整逐段稿 · Markdown','transcript.simplified.srt':'字幕（SRT）','transcript.md':'模型原始稿 · Markdown','transcript.txt':'模型原始稿 · TXT','transcript.srt':'模型原始字幕 · SRT','raw.json':'原始数据（JSON）'};
function exportDialog(){const d=state.detail;modal('导出这条录音',`<p>导出的副本可以自由编辑。正文为模型改写版，完整转写保留识别原文。</p>${Object.entries(artifactLabels).filter(([name])=>d.available.includes(name)).map(([name,label])=>`<div class="export-option"><span>${label}</span><a href="/api/download?id=${d.id}&name=${encodeURIComponent(name)}" download>下载 ↓</a></div>`).join('')}`);}
async function showLog(params){const result=await api('/api/log?'+new URLSearchParams(params));modal('运行记录',`<p>最近一段本地运行日志，用于定位具体原因。</p><pre>${escapeHTML(result.text)}</pre>`);}
async function uploadFiles(files){
 if(state.uploading){toast('上一批文件还在接收，请稍候。');return;}
 if(!files.length)return;
 state.uploading=true;
 modal('导入录音',`<p>文件只会复制到本机。接收完成后排队处理，相同内容自动复用。</p><div id="upload-list">${files.map((f,i)=>`<div class="upload-line" id="upload-${i}"><strong>${escapeHTML(f.name)}</strong><progress value="0" max="100" aria-label="${escapeHTML(f.name)}接收进度"></progress><small>等待接收 · ${(f.size/1024/1024).toFixed(1)} MB</small></div>`).join('')}</div><div class="actions"><button data-view="queue" data-action="close-modal">查看处理队列</button></div>`);
 let failed=0,reused=0;
 for(let i=0;i<files.length;i++){
  const file=files[i],row=$(`#upload-${i}`);
  try{
   if(file.size>2*1024**3)throw new Error('单个文件最多 2 GB');
   if(!file.size)throw new Error('文件为空，请等待下载完成');
   const result=await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST','/api/upload');xhr.setRequestHeader('X-Filename',encodeURIComponent(file.name));xhr.upload.onprogress=e=>{if(row&&e.lengthComputable){$('progress',row).value=e.loaded/e.total*100;$('small',row).textContent=`正在接收 ${Math.round(e.loaded/e.total*100)}%`;}};xhr.onerror=()=>reject(new Error('接收中断，请检查本地服务'));xhr.onload=()=>{try{const data=JSON.parse(xhr.responseText);xhr.status<300?resolve(data):reject(new Error(data.error));}catch{reject(new Error('无法接收文件'));}};xhr.send(file);});
   if(row){$('progress',row).value=100;$('small',row).textContent=result.reused?'内容已存在，已复用原稿':'已接收 · 等待本地处理';}if(result.reused)reused++;
  }catch(error){failed++;if(row)$('small',row).textContent='未导入：'+error.message;}
 }
 state.uploading=false;toast(failed?`${failed} 个文件未导入，请查看接收结果`:`已接收 ${files.length} 个文件${reused?`，其中 ${reused} 个复用已有原稿`:''}`,!!failed);await refresh();
}
async function action(name,button){
 if(await featureAction(name,button))return;
 const id=state.detail?.id;
 if(name==='agent'){agentDialog();return;}
 if(name==='launch-agent'){const parts=agentParts();if(!parts.length)throw new Error('请至少选择一项内容');const result=await act('agent',{id,agent:button.dataset.agent,parts});$('#modal').close();toast(result.found?'已在终端启动，Agent 读完上下文后即可开始提问':'已打开终端；未检测到对应命令时终端会给出安装方式');return;}
 if(name==='copy-agent-context'){const parts=agentParts();if(!parts.length)throw new Error('请至少选择一项内容');const result=await api('/api/agent-context?'+new URLSearchParams([['id',id],...parts.map(p=>['part',p])]));if(window.webkit?.messageHandlers?.tingjiClipboard){window.webkit.messageHandlers.tingjiClipboard.postMessage(result.text);}else{await navigator.clipboard.writeText(result.text);}toast('上下文已复制，可粘贴到 Agent 对话中');return;}
 if(name==='download-agent-context'){const parts=agentParts();if(!parts.length)throw new Error('请至少选择一项内容');location.href='/api/agent-context?'+new URLSearchParams([['id',id],['download','1'],...parts.map(p=>['part',p])]);return;}
 if(name==='save-speaker'){const speaker=$('#speaker-id').value,nameValue=$('#speaker-input').value.trim();if($('#remember-voice')?.checked||$('#speaker-me')?.checked)await act('remember_voice',{id,speaker,name:nameValue,me:$('#speaker-me').checked});else await act('rename_speaker',{id,speaker,name:nameValue});$('#modal').close();toast(nameValue?`已将全部发言同步为「${nameValue}」`:'已恢复默认编号');const fresh=await api(`/api/detail?id=${id}`);Object.assign(state.detail,{segments:fresh.segments,speakers:fresh.speakers});$('#speakers-panel').innerHTML=speakersPanel(state.detail);if(state.tab!=='notes')await renderDocument();return;}
 if(name==='regenerate-title'){await act('title',{id});toast('已加入队列，本地模型将根据转写内容重新生成名称');await refresh();return;}
 if(name==='batch-delete'){deleteDialog([...state.selected]);return;}
 if(name==='confirm-delete'){const ids=state.pendingDelete||[],deleteAudio=$('#delete-audio').checked;button.disabled=true;let trashed=0,kept=0,failed=0;for(const key of ids){try{const result=await act('delete',{id:key,delete_audio:deleteAudio});trashed+=result.trashed;kept+=result.kept.length;}catch(error){failed++;toast(error.message,true);}}$('#modal').close();state.pendingDelete=null;ids.forEach(key=>state.selected.delete(key));if(state.detail&&ids.includes(state.detail.id)){state.detail=null;state.view='library';history.replaceState(null,'','#library');}await refresh(true);if(!failed)toast(`已删除 ${ids.length} 条录音${deleteAudio?`，${trashed} 个源文件已移到废纸篓${kept?`，${kept} 个文件未改动`:''}`:'，录音源文件保留'}`);return;}
 if(name==='import'){$('#file-input').click();return;}
 if(name==='refresh'){await refresh(true);return;}
 if(name==='close-modal'){$('#modal').close();return;}
 if(name==='pause'){await act('pause',{paused:!state.data.paused});toast(state.data.paused?'已恢复后续任务':'后续任务将暂停，当前处理阶段继续完成');await refresh(true);return;}
 if(['scan','notify'].includes(name)){if(name==='scan')button.disabled=true;await act(name);toast(name==='scan'?'已开始扫描，发现新录音后自动转写':'操作已加入队列');await refresh(true);return;}
 if(name==='save-notes'){await saveNotes();return;}
 if(name==='refresh-detail'){await openDetail(id,state.tab);return;}
 if(name==='copy'){if(window.webkit?.messageHandlers?.tingjiClipboard){window.webkit.messageHandlers.tingjiClipboard.postMessage(state.docText);}else{await navigator.clipboard.writeText(state.docText);}toast('已复制');return;}
 if(name==='export'){exportDialog();return;}
 if(name==='history'){const result=await api('/api/history?id='+id);modal('历史文稿',result.versions.length?'<p>每次重新生成时，保留此前生成的文稿。可下载后比较。</p>'+result.versions.map(v=>`<div class="section-title">${date(v.created_at)} · ${v.id.slice(-6)}</div>${v.files.map(name=>`<div class="export-option"><span>${artifactLabels[name]}</span><a href="/api/download?id=${id}&name=${name}&version=${v.id}" download>下载历史版 ↓</a></div>`).join('')}`).join(''):'<p>暂时没有历史快照。下次重新生成文稿时，会自动保留当前版本。</p>');return;}
 if(name==='rename'){modal('重命名录音',`<p>修改工作台显示名称，原始文件和自动文稿保持原位。</p><input id="rename-input" maxlength="200" aria-label="录音名称" value="${escapeHTML(state.detail.title)}"><div class="actions"><button data-action="close-modal">取消</button><button class="primary" data-action="save-title">保存名称</button></div>`);$('#rename-input').focus();return;}
 if(name==='save-title'){const title=$('#rename-input').value.trim();if(!title)throw new Error('请输入录音名称');await act('metadata',{id,changes:{title}});$('#modal').close();state.detail.title=title;$('.detail-heading h1').textContent=title;toast('名称已保存');return;}
 if(name==='regenerate'){modal('重新生成文稿',`<p>重新生成正文与分析稿，复用原始转写。现有文稿会先保留为历史快照，个人笔记不受影响。</p><p>处理将加入队列，在当前模型任务结束后开始。</p><div class="actions"><button data-action="close-modal">返回</button><button class="primary" data-action="confirm-regenerate">加入生成队列</button></div>`);return;}
 if(name==='confirm-regenerate'){await act('regenerate',{id});$('#modal').close();toast('已加入文稿生成队列');await refresh();return;}
 if(name==='reveal'){await act('reveal',{id});return;}
 if(name==='archive'){await act('metadata',{id,changes:{archived:!state.detail.archived}});toast(state.detail.archived?'已恢复到资料库':'已归档，音频和文稿保留');navigate('library');await refresh(true);return;}
 if(name==='clear-selection'){state.selected.clear();renderRows();return;}
 if(name==='batch-archive'){for(const key of state.selected)await act('metadata',{id:key,changes:{archived:state.view!=='archive'}});state.selected.clear();await refresh(true);toast('资料已更新');return;}
 if(name==='batch-export'){const selected=[...state.selected];const unavailable=selected.filter(key=>!state.data.items.find(i=>i.id===key)?.available.includes('transcript.simplified.txt'));if(unavailable.length)throw new Error(`${unavailable.length} 条录音尚无完整转写，请先取消选择它们。`);location.href='/api/export?'+selected.map(key=>'id='+key).join('&');return;}
 if(name==='compatible-audio'){button.disabled=true;button.textContent='正在生成兼容音频…';const a=$('#audio');a.src=`/api/audio?id=${id}&compatible=1`;a.load();$('#audio-warning').textContent='正在本机转换为兼容音频，较长录音可能需要稍等。';a.addEventListener('loadedmetadata',()=>{$('#audio-warning').textContent='当前播放本地兼容音频，原始录音保持不变。';},{once:true});return;}
 if(name==='close-reference'){$('#reference').innerHTML='';return;}
}
document.addEventListener('click', async event=>{
 const el=event.target.closest('button,a[data-tab]');if(!el||el.disabled)return;
 try{
  if(el.dataset.view){if($('#modal').open)$('#modal').close();navigate(el.dataset.view);return;}
  if(el.dataset.open){await openDetail(el.dataset.open);return;}
  if(el.dataset.filter){state.filter=el.dataset.filter;state.selected.clear();renderRows();return;}
  if(el.dataset.tab){event.preventDefault();if(state.tab===el.dataset.tab)return;if(!canLeave())return;state.dirty=false;state.tab=el.dataset.tab;await renderDocument();return;}
  if(el.dataset.favorite){const key=el.dataset.favorite,item=state.detail?.id===key?state.detail:state.data.items.find(i=>i.id===key);await act('metadata',{id:key,changes:{favorite:!item.favorite}});item.favorite=!item.favorite;if(state.detail)el.textContent=item.favorite?'取消收藏':'收藏';await refresh();return;}
  if(el.dataset.delete){deleteDialog([el.dataset.delete]);return;}
  if(el.dataset.speaker){speakerDialog(el.dataset.speaker);return;}
  if(el.dataset.request){await act(el.dataset.operation,{id:el.dataset.request});toast(el.dataset.operation==='cancel'?'已取消等待中的请求；本地副本保留':'队列已更新');await refresh(true);return;}
  if(el.dataset.enrich){await act('enrich',{id:el.dataset.enrich});toast('已加入文稿生成队列，原稿保持可读');await refresh();return;}
  if(el.dataset.diarize){await act('diarize',{id:el.dataset.diarize});toast('已加入说话人区分队列，完成后更新正文与分析稿');await refresh();return;}
  if(el.dataset.retryAsr){await act('retry_asr',{id:el.dataset.retryAsr});toast('已加入转写队列');await refresh();return;}
  if(el.dataset.logRequest){await showLog({request:el.dataset.logRequest});return;}
  if(el.dataset.logId){await showLog({id:el.dataset.logId,...(el.dataset.text?{text:1}:{})});return;}
  if(el.dataset.ref){const related=state.detail.segments.filter(s=>(el.dataset.refKind==='turn'?s.id:(s.original_segment||s.id))===+el.dataset.ref);if(!related.length)return;const s={...related[0],text:related.map(s=>s.text).join(''),speaker_label:[...new Set(related.map(s=>s.speaker_label).filter(Boolean))].join('、')};$('#reference').innerHTML=`<aside class="reference-panel"><div class="ref-head"><span>对应原文 · ${escapeHTML(s.speaker_label||'')} ${timecode(s.start)}</span><button class="text-button" data-action="close-reference">收起 ×</button></div><p>${escapeHTML(s.text)}</p><button data-seek="${s.start}">播放这段录音 ↗</button></aside>`;$('#reference').scrollIntoView({behavior:'smooth',block:'center'});await seek(s.start);return;}
  if(el.dataset.seek!==undefined){await seek(+el.dataset.seek);return;}
  if(el.dataset.jump){await seek(($('#audio')?.currentTime||0)+Number(el.dataset.jump));return;}
  if(el.dataset.action){await action(el.dataset.action,el);}
 }catch(error){toast(error.message||'操作未完成，请重试',true);}
});
document.addEventListener('input',event=>{
 const el=event.target;
 if(el.id==='library-search'){state.query=el.value;state.selected.clear();renderRows();}
 if(el.id==='segment-search')$('#segments').innerHTML=segmentsHTML(state.detail.segments,el.value);
 if(el.id==='notes-editor'){state.dirty=true;$('#save-state').textContent='尚未保存';}
});
document.addEventListener('change',async event=>{
 const el=event.target;
 try{
  if(el.id==='sort'){state.sort=el.value;renderRows();}
  if(el.dataset.select){el.checked?state.selected.add(el.dataset.select):state.selected.delete(el.dataset.select);renderRows();}
  if(el.id==='select-all'){filteredItems().forEach(i=>el.checked?state.selected.add(i.id):state.selected.delete(i.id));renderRows();}
  if(el.id==='speed')$('#audio').playbackRate=+el.value;
  if(el.id==='version-select')await openDetail(el.value,state.tab);
  if(el.id==='file-input'){const files=[...el.files];el.value='';await uploadFiles(files);}
 }catch(error){toast(error.message,true);}
});
let dragCount=0;
window.addEventListener('dragenter',e=>{if(e.dataTransfer?.types.includes('Files')){e.preventDefault();dragCount++;$('#drop-overlay').hidden=false;}});
window.addEventListener('dragleave',e=>{e.preventDefault();if(--dragCount<=0){dragCount=0;$('#drop-overlay').hidden=true;}});
window.addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files'))e.preventDefault();});
window.addEventListener('drop',e=>{if(e.dataTransfer?.files.length){e.preventDefault();dragCount=0;$('#drop-overlay').hidden=true;uploadFiles([...e.dataTransfer.files]);}});
window.addEventListener('beforeunload',e=>{if(state.dirty||state.uploading){e.preventDefault();e.returnValue='';}});
document.addEventListener('keydown',e=>{
 if(e.key==='Enter'&&['speaker-input','rename-input'].includes(e.target.id)){e.preventDefault();action(e.target.id==='speaker-input'?'save-speaker':'save-title').catch(error=>toast(error.message,true));}
 if((e.metaKey||e.ctrlKey)&&e.key==='o'){e.preventDefault();$('#file-input').click();}
 if((e.metaKey||e.ctrlKey)&&e.key==='s'&&state.tab==='notes'&&state.detail){e.preventDefault();saveNotes().catch(error=>toast(error.message,true));}
 if((e.metaKey||e.ctrlKey)&&e.key==='k'&&$('#library-search')){e.preventDefault();$('#library-search').focus();}
});
async function boot(){
 const hash=location.hash.slice(1),params=new URLSearchParams(hash);
 try{
  if(params.has('token')){const result=await fetch('/api/session',{method:'POST',headers:{Authorization:'Bearer '+params.get('token')}});if(!result.ok)throw new Error('访问凭据已失效，请双击工作台入口重新打开。');history.replaceState(null,'','#library');}
  else if(names[hash])state.view=hash;
  await refresh(true);
  if(state.data&&params.get('recording'))await openDetail(params.get('recording'));
 }catch(error){$('#main').innerHTML=empty('请重新打开工作台',escapeHTML(error.message));}
 setInterval(()=>{if(!document.hidden)refresh();},4000);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
}
boot();
