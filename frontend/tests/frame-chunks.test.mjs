import assert from'node:assert/strict';import test from'node:test';import{build}from'esbuild';import{createRequire}from'node:module';
const b=await build({entryPoints:[new URL('../src/lib/frameChunks.ts',import.meta.url).pathname],bundle:true,write:false,format:'cjs',platform:'node'});const m={exports:{}};new Function('require','module','exports',b.outputFiles[0].text)(createRequire(import.meta.url),m,m.exports);const{readFrameChunk}=m.exports;
function pack(frames=[[10,3],[12,4]],revision='r',body=['abc','defg']){const meta=new TextEncoder().encode(JSON.stringify({revision,frames}));const size=new Uint8Array(4);new DataView(size.buffer).setUint32(0,meta.length);return new Blob([size,meta,...body]);}
test('frame chunks preserve exact JPEG bytes and frame IDs',async()=>{const parts=await readFrameChunk(pack(),'r',10,3,11);assert.deepEqual(parts.map(p=>p[0]),[10,12]);assert.equal(await parts[0][1].text(),'abc');assert.equal(await parts[1][1].text(),'defg');assert.equal(parts[0][1].type,'image/jpeg');});
for(const [name,blob]of[['version',pack(undefined,'stale')],['duplicate',pack([[10,3],[10,4]])],['outside',pack([[9,3],[12,4]])],['excluded',pack([[11,3],[12,4]])],['truncated',pack().slice(0,-1)],['oversized metadata',new Blob([new Uint8Array([255,255,255,255])])]])test('reject '+name+' before publishing any entries',async()=>{await assert.rejects(readFrameChunk(blob,'r',10,3,11));});
test('a complete neighboring JPEG is usable before the rest of its block arrives',async t=>{
 const originalFetch=globalThis.fetch;t.after(()=>globalThis.fetch=originalFetch);
 const all=new Uint8Array(await pack().arrayBuffer());const n=new DataView(all.buffer).getUint32(0);const boundary=4+n+3;
 let enqueueTail;const tail=new Promise(r=>enqueueTail=r);const delivered=[];
 globalThis.fetch=async()=>new Response(new ReadableStream({async start(controller){controller.enqueue(all.slice(0,boundary));await tail;controller.enqueue(all.slice(boundary));controller.close();}}),{headers:{'X-Media-Revision':'r'}});
 const promise=m.exports.fetchFrameChunk('/block','r',10,3,11,new AbortController().signal,e=>delivered.push(e));
 for(let i=0;i<20&&!delivered.length;i++)await new Promise(r=>setImmediate(r));
 assert.deepEqual(delivered.map(e=>e[0]),[10]);assert.equal(await delivered[0][1].text(),'abc');enqueueTail();await promise;assert.deepEqual(delivered.map(e=>e[0]),[10,12]);
});
