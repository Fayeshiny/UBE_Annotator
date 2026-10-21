import assert from 'node:assert/strict';import test from'node:test';import{build}from'esbuild';import{createRequire}from'node:module';
const b=await build({entryPoints:[new URL('../src/lib/videoDownloadPlan.ts',import.meta.url).pathname],bundle:true,write:false,format:'cjs',platform:'node'});const m={exports:{}};new Function('require','module','exports',b.outputFiles[0].text)(createRequire(import.meta.url),m,m.exports);
const{VideoDownloadPlan,rangesForFrames,VIDEO_CHUNK_BYTES:C}=m.exports;
test('download latest target then neighbors then fill holes; committed blocks never repeat',()=>{
 const p=new VideoDownloadPlan(C*10+17);p.prioritize([[C*8+100,C*8+150],[C*9,C*9+1],[C*7,C*7+1]]);
 assert.equal(p.next(),C*8);p.commit(p.next());assert.equal(p.next(),C*9);
 p.prioritize([[C*2,C*3+1],[C*8,C*8+1]]);assert.equal(p.next(),C*2);p.commit(p.next());assert.equal(p.next(),C*3);p.commit(p.next());assert.equal(p.next(),0);
 p.commit(0);assert.equal(p.next(),C);p.commit(C);assert.equal(p.next(),C*4);
});
test('frame-to-byte mapping uses keyframe groups rather than proportional file offsets',()=>{
 const index=[[0,29,100,3000],[30,59,3001,9000000],[60,89,9000001,9000500]];
 assert.deepEqual(rangesForFrames(index,[61,35,62,0]),[[9000001,9000500],[3001,9000000],[100,3000]]);
 assert.deepEqual(rangesForFrames(index,[99]),[]);
});
test('invalid ranges are ignored and offsets above 2 GiB remain exact',()=>{
 const p=new VideoDownloadPlan(2**31+17);p.prioritize([[-1,5],[5,4],[NaN,12],[2**31,2**31+16]]);assert.equal(p.next(),2**31);p.commit(p.next());assert.equal(p.next(),0);
});
