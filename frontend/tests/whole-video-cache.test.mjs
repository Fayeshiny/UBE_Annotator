import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { build } from 'esbuild';
const built = await build({entryPoints:[new URL('../src/lib/wholeVideoCache.ts',import.meta.url).pathname],bundle:true,write:false,platform:'node',format:'cjs'});
const mod={exports:{}};
new Function('require','module','exports',built.outputFiles[0].text)(createRequire(import.meta.url),mod,mod.exports);
const {WholeVideoCache}=mod.exports;
const chunk=4*1024*1024;
const tick=()=>new Promise(r=>setImmediate(r));
function fixture(options={}) {
 const states=[],requests=[],parts=[],revoked=[];
 let disposed=0,created=0,unauthorized=0;
 const total=options.total??chunk+7;
 const fetch=async(url,init)=>{
  requests.push(init);
  const headers={'Content-Length':String(total),'Accept-Ranges':'bytes','X-Media-Revision':'r1'};
  if(init.method==='HEAD')return new Response(null,{headers});
  const [start,end]=init.headers.Range.slice(6).split('-').map(Number);
  return options.response?.({start,end,init,headers})??new Response(new Blob([new Uint8Array(end-start+1).fill(start?2:1)]),{status:206,headers:{...headers,'Content-Range':`bytes ${start}-${end}/${total}`}});
 };
 const cache=new WholeVideoCache('/video?revision=r1','r1',s=>states.push(s),{
  fetch,estimate:async()=>options.estimate??{},storage:async()=>({append:async b=>{parts.push(b);await options.append?.(b);},complete:async()=>new Blob(parts),dispose:async()=>{disposed++;}}),
  createURL:b=>{created++;assert.equal(b.size,total);return 'blob:local';},revokeURL:u=>revoked.push(u),unauthorized:()=>unauthorized++, ...options.dependencies,
 });
 return {cache,states,requests,parts,revoked,get disposed(){return disposed;},get created(){return created;},get unauthorized(){return unauthorized;}};
}
test('sequential bounded ranges publish local URL only after full validation',async()=>{
 const f=fixture();await f.cache.start();
 assert.deepEqual(f.requests.slice(1).map(r=>r.headers.Range),[`bytes=0-${chunk-1}`,`bytes=${chunk}-${chunk+6}`]);
 assert.equal(f.states.at(-1).status,'complete');assert.equal(f.states.at(-1).loaded,chunk+7);
 assert.equal(f.states.filter(s=>s.url).length,1);assert.equal(f.created,1);
 assert.equal((new Uint8Array(await f.parts[1].arrayBuffer()))[0],2);
 assert.ok(f.requests.every(r=>r.credentials==='same-origin'&&r.cache==='no-store'));
 await f.cache.start();assert.equal(f.created,1);
 await f.cache.dispose();await f.cache.dispose();assert.deepEqual(f.revoked,['blob:local']);assert.equal(f.disposed,1);
});
test('pause before download retains metadata and resumes without refetching HEAD',async()=>{
 const f=fixture({total:10});f.cache.setPaused(true);const done=f.cache.start();await tick();
 assert.equal(f.states.at(-1).status,'paused');assert.equal(f.requests.length,1);
 f.cache.setPaused(false);await done;assert.equal(f.states.at(-1).status,'complete');
 await f.cache.dispose();
});
test('pause aborts an in-flight range; immediate resume does not become an error',async()=>{
 let first=true;
 const f=fixture({total:10,response:({init})=>{
  if(!first)return;first=false;
  return new Promise((_,reject)=>init.signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true}));
 }});
 const done=f.cache.start();await tick();f.cache.setPaused(true);f.cache.setPaused(false);await done;
 assert.equal(f.states.at(-1).status,'complete');assert.deepEqual(f.requests.slice(1).map(r=>r.headers.Range),['bytes=0-9','bytes=0-9']);
 await f.cache.dispose();
});
test('resume keeps committed chunks rather than downloading them again',async()=>{
 let release;const blocked=new Promise(r=>release=r);let n=0;
 const f=fixture({append:async()=>{if(++n===1)await blocked;}});const done=f.cache.start();await tick();
 f.cache.setPaused(true);release();await tick();assert.equal(f.states.at(-1).loaded,chunk);assert.equal(f.states.at(-1).status,'paused');
 f.cache.setPaused(false);await done;assert.equal(f.requests.length,3);await f.cache.dispose();
});
for(const [name,response] of [
 ['server ignoring Range',()=>new Response('bad',{status:200})],
 ['wrong byte range',({headers})=>new Response('bad',{status:206,headers:{...headers,'Content-Range':'bytes 1-3/10'}})],
 ['changed revision',({headers})=>new Response('bad',{status:206,headers:{...headers,'X-Media-Revision':'r2'}})],
 ['truncated body',({headers})=>new Response('bad',{status:206,headers:{...headers,'Content-Range':'bytes 0-9/10'}})],
])test(name+' never exposes partial video and removes storage',async()=>{
 const f=fixture({total:10,response});await f.cache.start();assert.equal(f.states.at(-1).status,'error');assert.equal(f.created,0);assert.equal(f.disposed,1);
});
test('quota rejection occurs before storage or full download',async()=>{
 const f=fixture({estimate:{quota:10,usage:9}});await f.cache.start();assert.equal(f.states.at(-1).status,'error');assert.match(f.states.at(-1).error,/存储空间不足/);assert.equal(f.requests.length,1);assert.equal(f.disposed,0);
});
test('401 dispatches logout and never publishes local video',async()=>{
 const f=fixture({dependencies:{fetch:async()=>new Response(null,{status:401})}});await f.cache.start();assert.equal(f.unauthorized,1);assert.equal(f.states.at(-1).status,'error');assert.equal(f.created,0);
});
test('leaving while paused releases storage and prevents late state updates',async()=>{
 const f=fixture();f.cache.setPaused(true);const done=f.cache.start();await tick();const count=f.states.length;
 await f.cache.dispose();await done;assert.equal(f.states.length,count);assert.equal(f.created,0);assert.equal(f.disposed,1);
});
test('files larger than 2 GiB retain exact byte offsets without whole-file buffers',async()=>{
 const total=2**31+17,requests=[],states=[];let bytes=0;
 const cache=new WholeVideoCache('/large','large',s=>states.push(s),{
  fetch:async(_,init)=>{
   const headers={'Content-Length':String(total),'Accept-Ranges':'bytes','X-Media-Revision':'large'};
   if(init.method==='HEAD')return new Response(null,{headers});
   const [start,end]=init.headers.Range.slice(6).split('-').map(Number);requests.push([start,end]);
   assert.equal(start,bytes);assert.ok(end-start+1<=chunk);
   return {ok:true,status:206,headers:new Headers({...headers,'Content-Range':`bytes ${start}-${end}/${total}`}),blob:async()=>({size:end-start+1})};
  },
  storage:async()=>({append:async part=>{bytes+=part.size;},complete:async()=>({size:bytes}),dispose:async()=>{}}),
  estimate:async()=>({}),createURL:file=>{assert.equal(file.size,total);return 'blob:large';},revokeURL:()=>{},
 });
 await cache.start();assert.equal(states.at(-1).status,'complete');assert.equal(bytes,total);
 assert.deepEqual(requests.at(-1),[2**31,2**31+16]);assert.equal(requests.length,513);await cache.dispose();
});
test('priority downloads write out of order and publish a byte-identical complete file',async()=>{
 const total=chunk*2+17,parts=new Map(),requests=[],states=[];
 const cache=new WholeVideoCache('/priority','r',s=>states.push(s),{
  fetch:async(_,init)=>{const headers={'Content-Length':String(total),'Accept-Ranges':'bytes','X-Media-Revision':'r'};
   if(init.method==='HEAD')return new Response(null,{headers});
   const[start,end]=init.headers.Range.slice(6).split('-').map(Number);requests.push(start);
   return new Response(new Blob([new Uint8Array(end-start+1).fill(start/chunk+1)]),{status:206,headers:{...headers,'Content-Range':`bytes ${start}-${end}/${total}`}});},
  estimate:async()=>({}),storage:async()=>({append:async()=>assert.fail('priority storage must use offsets'),write:async(offset,blob)=>parts.set(offset,blob),complete:async()=>new Blob([...parts].sort((a,b)=>a[0]-b[0]).map(p=>p[1])),dispose:async()=>{}}),
  createURL:file=>{assert.equal(file.size,total);return 'blob:priority';},revokeURL:()=>{},
 });
 cache.prioritize([[chunk*2,total-1]]);await cache.start();assert.deepEqual(requests,[chunk*2,0,chunk]);assert.equal(states.at(-1).status,'complete');assert.equal(states.at(-1).loaded,total);
 const file=new Blob([...parts].sort((a,b)=>a[0]-b[0]).map(p=>p[1]));assert.deepEqual([...new Uint8Array(await file.slice(chunk-1,chunk+1).arrayBuffer())],[1,2]);assert.deepEqual([...new Uint8Array(await file.slice(chunk*2-1,chunk*2+1).arrayBuffer())],[2,3]);await cache.dispose();
});
