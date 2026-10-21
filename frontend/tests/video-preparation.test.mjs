import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import test from 'node:test';
import {build} from 'esbuild';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
const b=await build({entryPoints:[new URL('../src/components/VideoPreparationNotice.tsx',import.meta.url).pathname],bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',external:['react','react/jsx-runtime']});
const m={exports:{}};new Function('require','module','exports',b.outputFiles[0].text)(createRequire(import.meta.url),m,m.exports);
const {default:Notice,preparationReady}=m.exports;
const c=status=>({case_id:'SYNTHETIC',media_status:'ready',segment_preparation:{status,ready:2,total:6,failed:status==='error'?1:0,error:status==='error'?'disk full':null}});
test('annotation entry opens only when all server segments are ready',()=>{
 for(const state of ['waiting','queued','processing','error'])assert.equal(preparationReady(c(state)),false);
 assert.equal(preparationReady(c('ready')),true);
 assert.equal(preparationReady({...c('ready'),media_status:'processing'}),false);
});
test('preparation screen shows persistent progress and does not mount video',()=>{
 const html=renderToStaticMarkup(React.createElement(Notice,{video:c('processing')}));
 assert.match(html,/2\/6/);assert.match(html,/全部就绪后自动开放标注/);assert.doesNotMatch(html,/<video/);
});
test('only reviewers see retry controls and error detail remains visible',()=>{
 const html=renderToStaticMarkup(React.createElement(Notice,{video:c('error'),canRetry:true}));
 assert.match(html,/disk full/);assert.match(html,/重试失败片段/);
 const owner=renderToStaticMarkup(React.createElement(Notice,{video:c('error')}));assert.doesNotMatch(owner,/重试失败片段/);
});
