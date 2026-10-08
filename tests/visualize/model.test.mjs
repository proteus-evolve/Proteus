import assert from 'node:assert/strict';
import test from 'node:test';
import {available,escapeHtml,format,extent,seriesPath,score,metricValue,axisValue,
  selectedEpisode,phaseShare} from '../../proteus/visualize/static/model.js';
test('missing values are unavailable, not zeros', () => {
  for (const v of [null,undefined,NaN,Infinity,true,'0']) assert.equal(available(v),false);
  assert.equal(available(0),true); assert.equal(format(null),'—'); assert.equal(format(0),'0');
});
test('line gaps do not imply interpolation', () => {
  const rows = [{x:0,v:1},{x:1,v:null},{x:2,v:2},{x:null,v:3},{x:4,v:4}];
  assert.equal(seriesPath(rows,r=>r.v,r=>r.x,v=>v),'M0.00,1.00  M2.00,2.00  M4.00,4.00');
});
test('arbitrary evaluator score ranges are supported', () => {
  assert.deepEqual(extent([null,20,500]),[0,500]);
  assert.deepEqual(extent([-20,0,40]),[-20,40]);
  assert.deepEqual(extent([null]),[0,1]);
  assert.deepEqual(extent([0]),[0,1]);
});
test('error zeros are never shown as valid score', () => {
  const row = {evaluations:{bad:{score:0,status:'error'},good:{score:0,status:'ok'}}};
  assert.equal(score(row,'bad'),null); assert.equal(score(row,'good'),0);
  assert.equal(score(row,'absent'),null);
});
test('multiple named instruments are not averaged', () => {
  const ep = {evaluations:{a:{score:3,status:'ok'},b:{score:200,status:'ok'}},distance:.2,index:2,calls:10,cost:null};
  assert.equal(metricValue(ep,'score','b'),200); assert.equal(metricValue(ep,'distance','a'),.2);
  assert.equal(axisValue(ep,'cost'),null); assert.equal(axisValue(ep,'episode'),2);
});
test('user supplied markup is escaped', () => {
  assert.equal(escapeHtml('<img onerror="x">'),'&lt;img onerror=&quot;x&quot;&gt;');
});
test('phase shares require actual numbers', () => {
  assert.equal(phaseShare([null,20]),null); assert.deepEqual(phaseShare([0,0]),[0,0]);
  assert.deepEqual(phaseShare([10,30]),[.25,.75]);
});
test('custom episode counts select actual records', () => {
  const run = {episodes:[{index:0},{index:1},{index:2}]};
  assert.equal(selectedEpisode(run,2).index,2); assert.equal(selectedEpisode(run,30).index,2);
});
