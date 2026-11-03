"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
// Reuse the browser/DOM fixtures without running the existing suite twice.
const fixture=fs.readFileSync(path.join(__dirname,"web_coaching_tests.js"),"utf8").split("const cases=[];")[0]
  .replace("showEvidence};", "showEvidence,pollReference,referencePosition,boardEvaluation,retryReferenceNow:()=>{referenceRetryAt=0;}};")+
  "\nmodule.exports={browser,snapshot,taughtSnapshot,flush};";
const moduleFixture={exports:{}};
vm.runInNewContext(fixture,{require,__dirname,module:moduleFixture,structuredClone,AbortController,TextDecoder,TypeError});
const {browser,snapshot,taughtSnapshot,flush}=moduleFixture.exports;
const cases=[];const test=(name,run)=>cases.push({name,run});
const body=ui=>JSON.parse(ui.requests.at(-1).options.body);
async function respond(ui,extra={}) {
  const request=body(ui);
  await ui.finish({source:"stockfish_coaching_reference",status:"ready",request_id:request.request_id,fen:request.fen,
    cp:234,mate:null,bound:"exact",depth:18,...extra});
}

test("both colors use Stockfish white score while native classification stays unchanged",async()=>{
  const report=snapshot({status:"completed"}),before=JSON.stringify(report),ui=browser();await ui.start(report);
  void ui.api.pollReference();await flush();
  assert.equal(ui.requests.at(-1).url,"/api/coaching-reference");
  assert.equal(body(ui).history_uci.join(),"e1g1");
  await respond(ui,{cp:-234});
  assert.match(ui.get("coaching-score").textContent,/Stockfish.*-2.34/);
  assert.ok(parseFloat(ui.get("coaching-evaluation-white").style.height)<50);
  await ui.click("coaching-before");void ui.api.pollReference();await flush();
  assert.equal(body(ui).history_uci.length,0);
  await respond(ui,{cp:123});
  assert.match(ui.get("coaching-score").textContent,/Stockfish.*\+1.23/);
  assert.equal(JSON.stringify(report),before);
});
test("later PV position gets its own history and score, never the root score",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));
  await ui.click("coaching-best-line");await ui.click("coaching-line-next");
  assert.doesNotMatch(ui.get("coaching-score").textContent,/\+1.00/);
  void ui.api.pollReference();await flush();assert.equal(body(ui).history_uci.join(),"e1g1");
  await respond(ui,{cp:-350});assert.match(ui.get("coaching-score").textContent,/Stockfish.*-3.50/);
});
test("late POST and stale request id cannot populate a different position",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));
  void ui.api.pollReference();await flush();const stale=body(ui);
  await ui.click("coaching-before");
  await ui.finish({status:"ready",request_id:stale.request_id,fen:stale.fen,cp:9900});
  assert.doesNotMatch(ui.get("coaching-score").textContent,/99.00/);
  void ui.api.pollReference();await flush();
  await respond(ui,{request_id:stale.request_id,cp:8800});
  assert.doesNotMatch(ui.get("coaching-score").textContent,/88.00/);
  assert.match(ui.get("coaching-score").textContent,/Stockfish 평가 사용 불가/);
  assert.equal(ui.get("coaching-evaluation-white").style.height,"50%");
});
test("polling only accepts completed matching reference and failures keep the gauge neutral",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));void ui.api.pollReference();await flush();
  const context=body(ui);await ui.finish({...context,status:"loading",cp:9900});
  assert.doesNotMatch(ui.get("coaching-score").textContent,/99.00/);
  void ui.api.pollReference();await flush();assert.equal(ui.requests.at(-1).options.method,"GET");
  await ui.finish({...context,status:"unavailable",cp:null});
  assert.match(ui.get("coaching-score").textContent,/Stockfish 평가 사용 불가/);
  assert.equal(ui.get("coaching-evaluation-white").style.height,"50%");
});
test("retry hides the score immediately and late replies do not reveal answers",async()=>{
  const ui=browser();await ui.start(taughtSnapshot());void ui.api.pollReference();await flush();const context=body(ui);
  await ui.click("coach-retry");
  await ui.finish({...context,status:"ready",cp:9999});
  assert.match(ui.get("coaching-score").textContent,/가렸/);
  assert.ok(ui.get("coaching-evaluation-bar").classList.contains("unknown"));
  void ui.api.pollReference();await flush();assert.equal(body(ui).hidden,true);
  await ui.finish({status:"hidden",request_id:context.request_id,cp:null});
  assert.match(ui.get("coaching-score").textContent,/가렸/);
});
test("mate winner and board orientation stay white based",async()=>{
  for(const mate of [-3,4]){
    const ui=browser();await ui.start(snapshot({status:"completed"}));void ui.api.pollReference();await flush();
    await respond(ui,{cp:null,mate,winner:mate<0?"black":"white"});
    assert.equal(ui.get("coaching-evaluation-white").style.height,mate<0?"0%":"100%");
    await ui.click("coaching-flip");
    assert.equal(ui.get("coaching-evaluation-white").style.height,mate<0?"0%":"100%");
    assert.match(ui.get("coaching-score").textContent,/Stockfish.*메이트/);
  }
});
test("temporary failure retries after backoff at most twice and recovers",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));
  void ui.api.pollReference();await flush();const original=body(ui);
  await respond(ui,{status:"error",cp:null});
  const count=ui.requests.length;await ui.api.pollReference();assert.equal(ui.requests.length,count);
  ui.api.retryReferenceNow();void ui.api.pollReference();await flush();
  assert.notEqual(body(ui).request_id,original.request_id);await respond(ui,{cp:88});
  assert.match(ui.get("coaching-score").textContent,/Stockfish.*0.88/);
  const done=ui.requests.length;await ui.api.pollReference();assert.equal(ui.requests.length,done);
  const other=browser();await other.start(snapshot({status:"completed"}));
  await other.click("coaching-best-line");await other.click("coaching-line-next");
  for(let i=0;i<3;i++){other.api.retryReferenceNow();void other.api.pollReference();await flush();await respond(other,{status:"unavailable",cp:null});}
  const stopped=other.requests.length;other.api.retryReferenceNow();await other.api.pollReference();assert.equal(other.requests.length,stopped);
  assert.match(other.get("coaching-score").textContent,/Stockfish 평가 사용 불가.*불러오지 못했습니다/);
  assert.doesNotMatch(other.get("coaching-score").textContent,/계산 대기|재시도/);
});
test("leaving coaching rejects reference replies and cancels owned reference context",async()=>{
  const ui=browser();await ui.start(snapshot({status:"completed"}));void ui.api.pollReference();await flush();const context=body(ui);
  await ui.api.leave();await ui.finish({...context,status:"ready",cp:9999});
  assert.equal(ui.get("coaching-view").hidden,true);
  void ui.api.pollReference();await flush();assert.equal(body(ui).hidden,true);
});

(async()=>{for(const {name,run} of cases){await run();console.log("PASS "+name);}console.log(cases.length+" coaching reference browser checks passed.");})().catch(error=>{console.error(error);process.exitCode=1;});
