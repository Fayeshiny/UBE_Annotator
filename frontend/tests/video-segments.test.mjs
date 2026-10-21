import assert from 'node:assert/strict';
import test from 'node:test';
import {build} from 'esbuild';
import {createRequire} from 'node:module';
const b=await build({entryPoints:[new URL('../src/lib/videoSegments.ts',import.meta.url).pathname],bundle:true,write:false,format:'cjs',platform:'node'});
const m={exports:{}};new Function('require','module','exports',b.outputFiles[0].text)(createRequire(import.meta.url),m,m.exports);
const {editSegment}=m.exports;
const base=()=>({context:[{start_ms:0,label:'C1'}],evaluable:[{start_ms:0,label:'E1'}],samples:{0:{note:'before'},300000:{note:'core'},600000:{note:'after'}},events:[{id:'E1',onset_ms:290000,offset_ms:null,recovery_ms:null,note:''}],milestones:{M1:{time_ms:1000,assessable:1}}});
const s={start_ms:300000,end_ms:600000};
test('state labels edited in the child preserve earlier and later parent states',()=>{
 const a=base();const n=editSegment(a,{...a,context:[{start_ms:0,label:'C2'},{start_ms:450000,label:'C3'}]},s);
 assert.deepEqual(n.context,[{start_ms:0,label:'C1'},{start_ms:300000,label:'C2'},{start_ms:450000,label:'C3'},{start_ms:600000,label:'C1'}]);
});
test('context samples and milestones are read-only, samples keep global grid keys',()=>{
 const a=base();const n=editSegment(a,{...a,samples:{0:{note:'bad'},300000:{note:'new'},600000:{note:'bad'}},milestones:{M1:{time_ms:1000,assessable:0}}},s);
 assert.equal(n.samples[0].note,'before');assert.equal(n.samples[300000].note,'new');assert.equal(n.samples[600000].note,'after');assert.equal(n.milestones.M1.assessable,1);
});
test('cross-boundary event retains identity/onset while ending in current child',()=>{
 const a=base();const n=editSegment(a,{...a,events:[{...a.events[0],onset_ms:280000,offset_ms:310000,recovery_ms:320000}]},s);
 assert.equal(n.events[0].id,'E1');assert.equal(n.events[0].onset_ms,290000);assert.equal(n.events[0].offset_ms,310000);
 assert.equal(editSegment(a,{...a,events:[]},s).events.length,1);
});
test('final subtask adds no state boundary beyond parent duration; unassessable milestones remain editable',()=>{
 const a=base();a.milestones={};const final={start_ms:300000,end_ms:600000};
 const n=editSegment(a,{...a,context:[{start_ms:0,label:'C2'}],milestones:{M2:{time_ms:null,assessable:0}}},final,600000);
 assert.equal(n.context.at(-1).start_ms,300000);assert.equal(n.milestones.M2.assessable,0);
});
test('recovery may be marked in a later child after event offset in an earlier child',()=>{
 const a=base();a.events[0].offset_ms=295000;
 const n=editSegment(a,{...a,events:[{...a.events[0],recovery_ms:310000}]},s);
 assert.equal(n.events[0].onset_ms,290000);assert.equal(n.events[0].offset_ms,295000);assert.equal(n.events[0].recovery_ms,310000);
});
