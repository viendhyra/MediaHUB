// Проверяем реальные функции интерфейса с ответами сервера, без запуска установки.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../templates/index.html'),'utf8');
const source=html.slice(html.indexOf('async function loadHubUpdate(force='),html.indexOf('$("hubCheckUpdate").onclick='));
function harness(responses){
 const elements={},calls=[],context={hubUpdateData:null,hubUpdatePoll:null,hubUpdateStatus:null,activePage:'setup',setTimeout:()=>1,clearTimeout:()=>{},$:id=>elements[id]??=( {textContent:'',disabled:false,classList:{toggle(name,on){this[name]=on},add(name){this[name]=true}}}),fetch:async url=>{calls.push(url);assert.ok(responses.length,'Лишний запрос '+url);return {ok:true,json:async()=>responses.shift()}}};
 vm.createContext(context);vm.runInContext(source,context);return {context,elements,calls};
}
(async()=>{
 const old={status:'complete',message:'Установлена версия 22.10. Резервная копия: /backup/old',log:'старый журнал'};
 for(const available of [false,true]){
  const {context,elements,calls}=harness([{currentVersion:'22.13',latestVersion:available?'22.14':'22.12',available},old]);
  await context.loadHubUpdate();assert.match(elements.hubUpdateInfo.textContent,/Установлена 22\.13\./);assert.equal(elements.hubUpdateState.textContent,old.message);assert.equal(elements.hubUpdateLog.textContent,old.log);assert.equal(elements.hubInstallUpdate.classList.hidden,!available);assert.equal(calls.length,2);
 }
 for(const status of ['complete','error']){
  const currentVersion=status==='complete'?'22.14':'22.13';
  const {context,elements,calls}=harness([{status:'running',message:'Установка'}, {status,message:'Результат'}, {currentVersion,latestVersion:'22.14',available:status==='error'}, {status,message:'Результат'}]);
  await context.loadHubUpdateStatus();await context.loadHubUpdateStatus();assert.match(elements.hubUpdateInfo.textContent,new RegExp('Установлена '+currentVersion.replace('.','\\.')));assert.equal(elements.hubInstallUpdate.classList.hidden,status==='complete');assert.equal(calls.filter(url=>url==='/api/updates?force=true').length,1);
 }
 const {context,elements}=harness([{currentVersion:'22.13',available:false,error:'GitHub недоступен'},{status:'idle'}]);
 await context.loadHubUpdate();assert.equal(elements.hubUpdateInfo.textContent,'Установлена 22.13. GitHub недоступен');
 console.log('Проверки интерфейса обновлений: 5 сценариев прошли');
})().catch(error=>{console.error(error);process.exitCode=1});
