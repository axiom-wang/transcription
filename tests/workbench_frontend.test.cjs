// Pure offline rendering/interaction-contract checks. No browser and no network.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../src/audio_notes/web/app.js'),'utf8').replace(/boot\(\);\s*$/, '');
function fixture(){
 const elements=new Map(),events={};
 function element(key){if(!elements.has(key))elements.set(key,{innerHTML:'',textContent:'',hidden:false,open:false,value:'',classList:{add(){},remove(){},toggle(){}},addEventListener(){},setAttribute(){},focus(){},scrollIntoView(){},showModal(){this.open=true;},close(){this.open=false;},querySelector(){return element('nested');}});return elements.get(key);}
 const item={id:'a'.repeat(64),audio_hash:'b'.repeat(64),title:'测试录音 <img src=x onerror=alert(1)>',created:1700000000,updated:1700000000,recorded_at:null,duration:12,source_kind:'手动导入',asr_status:'completed',text_status:'failed',asr_error:null,text_error:'本次分析失败',available:['transcript.simplified.md','transcript.simplified.txt','polished.md','analysis.md'],text_available:false,favorite:false,archived:false,needs_review:false,progress:{},attempts:1,preview:'只是一项建议，尚未决定。',versions:[{id:'a'.repeat(64),created:1700000000,asr_status:'completed'}],segments:[{id:1,start:0,end:3,text:'原文 <script>alert(1)</script>'},{id:2,start:3,end:6,text:'只是提议，尚未决定。'}],source:'/tmp/test.wav',output:'/tmp/output',model:'synthetic',notes:'独立笔记',revision:2,audio_name:'test.wav'};
 const data={items:[item],requests:[{id:'c'.repeat(32),title:'排队 <script>x</script>',status:'queued',kind:'transcribe',created:1700000000,priority:0}],backlog:[{name:'待扫描录音',size:123,settling:true}],paused:false,busy:false,pipeline_busy:false,scan:{},legacy_service:{removed:false},now:1700000000,settings:{model:'test',language:'zh',analysis_enabled:true,output:'/tmp/output',config:'/tmp/config.toml'}};
 const context={console,URLSearchParams,Date,JSON,Set,Map,Promise,Math,Number,String,Object,Array,encodeURIComponent,clearTimeout(){},setTimeout(){},setInterval(){},confirm:()=>true,document:{hidden:false,querySelector:element,querySelectorAll:()=>[],addEventListener:(name,fn)=>events[name]=fn},window:{addEventListener:(name,fn)=>events[name]=fn,scrollTo(){}},history:{replaceState(){}},location:{hash:'',href:''},navigator:{clipboard:{writeText:async()=>{}}},fetch:async()=>{throw Error('Network prohibited in offline frontend tests');}};
 vm.createContext(context);vm.runInContext(source,context);context.testData=data;context.testItem=item;
 vm.runInContext('state.data=testData;state.detail=null;',context);
 return {run:code=>vm.runInContext(code,context),elements,element,context,events,data,item};
}
test('all top-level views render with partial completion and escaped recording names',()=>{
 const f=fixture();
 for(const view of ['library','favorites','archive','queue','attention','settings']){
  f.run(`state.view='${view}';renderMain();`);
  assert.ok(f.element('#main').innerHTML.length>50);
  assert.ok(!f.element('#main').innerHTML.includes('<script>x</script>'));
 }
 f.run("state.view='library';renderLibrary();");
 const table=f.element('#library-table').innerHTML;
 assert.ok(table.includes('&lt;img'));
 assert.ok(table.includes('原稿可读'));
 assert.ok(table.includes('文稿待重试'));
});
test('markdown escapes HTML, blocks script links, and makes source citations actionable',()=>{
 const f=fixture();
 const html=f.run('markdown("## 标题\\n\\n<script>alert(1)</script>\\n\\n[点击](javascript:alert) [00:03](transcript.simplified.md#segment-2)")');
 assert.ok(!html.includes('<script>'));
 assert.ok(!html.includes('href="javascript:'));
 assert.ok(html.includes('data-ref="2"'));
 assert.ok(html.includes('<h2>标题</h2>'));
});
test('original transcript search retains timestamps and never renders source as HTML',()=>{
 const f=fixture();
 const html=f.run('segmentsHTML(testItem.segments)');
 assert.ok(html.includes('data-seek="3"'));
 assert.ok(html.includes('&lt;script&gt;'));
 assert.ok(!html.includes('<script>'));
 const result=f.run('segmentsHTML(testItem.segments,"提议")');
 assert.ok(result.includes('segment-2'));
 assert.ok(!result.includes('segment-1'));
});
test('detail notes editor preserves saved content and revision',async()=>{
 const f=fixture();
 await f.run("state.detail=testItem;state.tab='notes';renderDocument();");
 assert.ok(f.element('#document').innerHTML.includes('独立笔记'));
 assert.ok(f.element('#document').innerHTML.includes('第 2 版'));
 assert.ok(f.element('#document').innerHTML.includes('保存笔记'));
});
test('saving a note carries revision and does not clear unsaved text on conflict',async()=>{
 const f=fixture();
 f.context.fetch=async(_url,options)=>{const body=JSON.parse(options.body);assert.equal(body.changes.revision,2);assert.equal(body.changes.notes,'新笔记');return{ok:false,json:async()=>({error:'笔记已在其他窗口更新'})};};
 f.element('#notes-editor').value='新笔记';
 f.run("state.detail=testItem;state.tab='notes';state.dirty=true;");
 await assert.rejects(f.run('saveNotes()'),/其他窗口/);
 assert.equal(f.run('state.dirty'),true);
 assert.equal(f.element('#notes-editor').value,'新笔记');
});
test('completed document renders locally with source reference links',async()=>{
 const f=fixture();
 f.context.fetch=async()=>({ok:true,json:async()=>({text:'# 整理稿\n\n这是整理后的内容。[00:00](transcript.simplified.md#segment-1)'})});
 await f.run("state.detail=testItem;state.tab='polished';renderDocument();");
 assert.ok(f.element('#document').innerHTML.includes('整理后的内容'));
 assert.ok(f.element('#document').innerHTML.includes('data-ref="1"'));
});
test('progress has measured units and no invented percentage for unknown work',()=>{
 const f=fixture();
 assert.ok(!f.run("progressHTML({stage:'加载模型',updated:1700000000})").includes('<progress'));
 assert.ok(f.run("progressHTML({completed:2,total:6,unit:'段',updated:1700000000})").includes('已完成 2 / 6 段'));
});
test('filtering preserves available original when text generation failed',()=>{
 const f=fixture();f.run("state.view='library';state.filter='ready';state.query='';");
 assert.equal(f.run('filteredItems().length'),1);
 f.run("state.filter='issues';");assert.equal(f.run('filteredItems().length'),1);
 f.run("state.query='找不到的录音';");assert.equal(f.run('filteredItems().length'),0);
});
test('unsaved notes prevent navigation when user chooses to stay',()=>{
 const f=fixture();f.context.confirm=()=>false;
 f.run("state.detail=testItem;state.view='detail';state.dirty=true;navigate('library');");
 assert.equal(f.run('state.view'),'detail');assert.equal(f.run('state.dirty'),true);
});

test('scan button reflects a running scan and completion shows the round summary',()=>{
 const f=fixture();
 f.run("state.data.requests=[{id:'s'.repeat(32),kind:'scan',title:'扫描',status:'running',created:1700000000,priority:0}];state.view='library';renderLibrary();");
 assert.ok(f.element('#main').innerHTML.includes('data-action="scan" data-label="立即扫描" disabled>扫描中…'));
 f.run("state.view='settings';renderMain();");
 assert.ok(f.element('#main').innerHTML.includes('手动扫描语音备忘录'));
 assert.ok(!f.element('#main').innerHTML.includes('每 60 秒'));
 f.run("const before=new Set(['s'.repeat(32)]);state.data.requests=[{id:'s'.repeat(32),kind:'scan',title:'扫描',status:'completed',summary:'发现 1 条新录音 · 已转写 1 条 · 失败 0 条',created:1700000000,priority:0}];announceScans(before);");
 assert.equal(f.element('#toast').textContent,'扫描完成：发现 1 条新录音 · 已转写 1 条 · 失败 0 条');
 f.run("state.view='library';renderLibrary();");
 assert.ok(f.element('#main').innerHTML.includes('data-action="scan" data-label="立即扫描">立即扫描'));
});
