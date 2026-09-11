import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
const context={};vm.runInNewContext(await readFile(new URL('../tools/experiment05/static05/viz.js',import.meta.url),'utf8'),context);
const {finite,validGeo,viewport,geographicFrame,pathData,interval}=context.Experiment05VizMath;
test('nulls and numeric strings never become fabricated spatial points',()=>{
  for(const value of [null,undefined,NaN,Infinity,'0'])assert.equal(finite(value),false);
  assert.equal(validGeo({lat:null,lon:0}),false);assert.equal(validGeo({lat:90.1,lon:0}),false);assert.equal(validGeo({lat:43.7,lon:-79.4}),true);
  assert.equal(geographicFrame([{lat:null,lon:null}]),null);
});
test('geographic map is north-up and preserves metre scale with longitude latitude correction',()=>{
  const origin={lat:43.7,lon:-79.4},north={lat:43.701,lon:-79.4},east={lat:43.7,lon:-79.4+.001/Math.cos(43.7*Math.PI/180)};
  const frame=geographicFrame([origin,north,east]);
  const o=frame.pixel(origin),n=frame.pixel(north),e=frame.pixel(east);
  assert(n.y<o.y);assert(e.x>o.x);assert(Math.abs(n.x-o.x)<1e-8);assert(Math.abs(e.y-o.y)<1e-8);
  assert(Math.abs((o.y-n.y)/(e.x-o.x)-1)<.0001);
});
test('horizontal pan reaches both exact bounds without changing observation clocks',()=>{
  const bounds={start_t:100,end_t:1000},first=viewport(bounds,'300',0),last=viewport(bounds,'300',1000);
  assert.equal(first.start,100);assert.equal(first.end,400);assert.equal(last.start,700);assert.equal(last.end,1000);assert.equal(first.full,false);
  const full=viewport(bounds,'full',700);assert.equal(full.start,100);assert.equal(full.end,1000);assert.equal(full.full,true);
  assert.equal(viewport(bounds,'7200',300).width,900);assert.deepEqual(bounds,{start_t:100,end_t:1000});
});
test('single timestamp or malformed viewport has finite drawable extent',()=>{
  for(const bounds of [{start_t:42,end_t:42},{start_t:null,end_t:null},{start_t:4,end_t:2}]){
    const value=viewport(bounds,'full');assert(value.end>value.start);assert(Number.isFinite(value.start)&&Number.isFinite(value.end));
  }
});
test('invalid progress breaks paths instead of bridging or inventing zero',()=>{
  const points=[{t:10,s_m:0},{t:20,s_m:100},{t:30,s_m:null},{t:40,s_m:300}];
  assert.equal(pathData(points,x=>x,y=>y),'M10.00,0.00 L20.00,100.00  M40.00,300.00');
});
test('scheduled dwell keeps distinct original timestamp vertices at identical progress',()=>{
  const path=pathData([{t:100,s_m:900},{t:130,s_m:900},{t:200,s_m:1200}],x=>x,y=>y);
  assert.equal(path,'M100.00,900.00 L130.00,900.00 L200.00,1200.00');
});
test('APC interval respects raw start/end and rejects reversed intervals',()=>{
  assert.equal(JSON.stringify(interval({arrival:10,departure:40,t:10})),'[10,40]');
  assert.equal(JSON.stringify(interval({arrival:null,departure:40,t:null})),'[40,40]');
  assert.equal(interval({arrival:50,departure:40,t:50}),null);assert.equal(interval({}),null);
});
