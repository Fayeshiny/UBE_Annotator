import assert from 'node:assert/strict';import test from 'node:test';import{build}from'esbuild';import{createRequire}from'node:module';
const b=await build({entryPoints:[new URL('../src/lib/videoErrors.ts',import.meta.url).pathname],bundle:true,write:false,format:'cjs',platform:'node'});const m={exports:{}};new Function('require','module','exports',b.outputFiles[0].text)(createRequire(import.meta.url),m,m.exports);
const{videoErrorMessage,resetFailedVideo}=m.exports;
test('network interruption and decode failure are reported separately',()=>{
 assert.match(videoErrorMessage(2),/传输中断/);assert.match(videoErrorMessage(3),/解码失败/);assert.match(videoErrorMessage(4),/格式不受支持/);
});
test('errored resources are reloaded before retry; healthy media keeps its buffer',()=>{
 let loads=0;resetFailedVideo({error:{code:2},load(){loads++;}});assert.equal(loads,1);
 resetFailedVideo({error:null,load(){loads++;}});assert.equal(loads,1);
});
