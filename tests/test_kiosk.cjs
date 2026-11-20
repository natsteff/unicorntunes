// Deterministic lifecycle checks; no browser or third-party test packages required.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function kiosk() {
    let now=1000, busy=false, resets=0, clears=0, ready=0, errors=0, done;
    const events={}, intervals=[];
    const document={hidden:false,addEventListener:(name,fn)=>{events[name]=fn;}};
    const window={addEventListener:(name,fn)=>{events[name]=fn;},setInterval:fn=>intervals.push(fn)};
    vm.runInNewContext(fs.readFileSync('web/kiosk.js','utf8'),{window,document,Date:{now:()=>now}});
    const session=window.createKioskSession({isBusy:()=>busy,reset:fn=>{resets++;done=fn;},
        onReset:()=>clears++,onReady:()=>ready++,onError:()=>errors++});
    return {session,events,set busy(v){busy=v;},advance:ms=>{now+=ms;intervals.forEach(fn=>fn());},
        finish:error=>done(error),get resets(){return resets;},get clears(){return clears;},get ready(){return ready;},get errors(){return errors;}};
}
test('initialization and an idle reset require a confirmed new identity',()=>{
    const k=kiosk();assert.equal(k.session.ensure('csrf'),false);assert.equal(k.resets,1);
    k.advance(60000);assert.equal(k.resets,1);assert.equal(k.session.isReady(),false);
    k.finish();assert.equal(k.session.isReady(),true);k.session.activity();
    // Polling/ensure must not count as human activity.
    for(let i=0;i<24;i++){k.session.ensure('csrf');k.advance(5000);}
    assert.equal(k.resets,2);assert.equal(k.clears,2);assert.equal(k.session.isReady(),false);
    k.finish();assert.equal(k.ready,2);k.advance(600000);assert.equal(k.resets,2);
});
test('typing extends the visitor session; pending actions defer resetting',()=>{
    const k=kiosk();k.session.ensure('csrf');k.finish();k.session.activity();
    k.advance(119000);k.events.input();k.advance(119000);assert.equal(k.resets,1);
    k.busy=true;k.advance(10000);assert.equal(k.resets,1);
    k.busy=false;k.session.activity();k.advance(1000);assert.equal(k.resets,1);
    k.advance(120000);assert.equal(k.resets,2);
});
test('failed reset stays locked and retries after backoff, including after wake',()=>{
    const k=kiosk();k.session.ensure('csrf');k.finish('offline');
    assert.equal(k.errors,1);assert.equal(k.session.isReady(),false);
    k.advance(4999);assert.equal(k.resets,1);k.advance(1);assert.equal(k.resets,2);
    k.finish();k.session.activity();k.advance(121000);assert.equal(k.resets,3);k.finish();
    k.session.ensure('new-csrf');assert.equal(k.resets,4);assert.equal(k.session.isReady(),false);
});
test('Next guest cannot race a pending operation or duplicate a reset',()=>{
    const k=kiosk();k.session.ensure('csrf');k.finish();
    k.busy=true;k.session.next();assert.equal(k.resets,1);
    k.busy=false;k.advance(1000);k.session.next();assert.equal(k.resets,2);
    k.finish();assert.equal(k.session.isReady(),true);
});
