// Run: node tests/labeler_ui.test.cjs (uses existing frontend jsdom dependency).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require(require.resolve('jsdom', { paths: [process.argv[2] || path.join(__dirname, '../frontend')] }));
const page = fs.readFileSync(path.join(__dirname, '../src/trace_engine/labeler_web/index.html'), 'utf8');
let posted;
const dom = new JSDOM(page, {
  runScripts: 'dangerously', url: 'http://127.0.0.1:8765',
  beforeParse(window) {
    window.confirm = () => true;
    window.HTMLCanvasElement.prototype.setPointerCapture = () => {};
    window.HTMLCanvasElement.prototype.getContext = function () {
      return { createImageData: (w,h) => ({data:new Uint8ClampedArray(w*h*4)}),
        putImageData(){}, clearRect(){}, setLineDash(){}, beginPath(){}, moveTo(){}, lineTo(){}, stroke(){}, strokeRect(){}, closePath(){}, fill(){}, fillText(){} };
    };
    window.HTMLCanvasElement.prototype.toDataURL = () => 'data:image/png;base64,mask';
    window.fetch = async (url, options) => {
      if(options?.method==='POST'){posted=JSON.parse(options.body);return {ok:true,json:async()=>({revision:1,annotation_complete:posted.reviewed})}}
      return {ok:true,json:async()=>({plans:[],room_classes:['Background','Outdoor','Wall'],icon_classes:['No Icon','Window','Door'],vector_classes:['Outdoor','Kitchen','Living Room'],training_path:'test',coco_path:'test'})};
    };
  }
});
const w=dom.window;
const turn=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await turn();
  w.eval("current='test'; width=24; height=16; masks={rooms:new Uint8Array(384).fill(255),icons:new Uint8Array(384).fill(255)}; library=[{id:'test',name:'fixture',style:'line'}]; paint.width=24;paint.height=16;paint.getBoundingClientRect=()=>({left:0,top:0,width:24,height:16});");
  w.eval("$('task').value='rooms';labelOptions();");
  w.document.getElementById('tool').value='rectangle';
  w.document.getElementById('label').value='2';
  const canvas=w.document.getElementById('gesture');
  canvas.dispatchEvent(new w.MouseEvent('pointerdown',{clientX:2,clientY:3,button:0}));
  canvas.dispatchEvent(new w.MouseEvent('pointerup',{clientX:7,clientY:5,button:0}));
  assert.equal(w.eval('masks.rooms[3*24+2]'),2);
  assert.equal(w.eval('masks.rooms[0]'),255);
  assert.equal(w.eval('masks.icons[3*24+2]'),255);
  assert.equal(w.eval('dirty'),true);
  w.document.getElementById('undo').click();
  assert.equal(w.eval('masks.rooms.every(v=>v===255)'),true);
  w.eval('points=[{x:2,y:2},{x:10,y:2},{x:10,y:10},{x:2,y:10}]; finish();');
  assert.equal(w.eval('masks.rooms[5*24+5]'),2);
  assert.equal(w.eval('masks.rooms[0]'),255);
  w.document.getElementById('export').click();
  assert.match(w.document.getElementById('status').textContent,/Check both layers/);
  assert.equal(posted,undefined);
  w.document.getElementById('reviewed').checked=true;
  w.document.getElementById('export').click();
  await turn();
  assert.equal(posted.reviewed,true);
  assert.equal(posted.revision,0);
  assert.equal(w.eval('dirty'),false);
  w.eval('remember(); dab({x:12,y:12}); changed();');
  assert.equal(w.document.getElementById('reviewed').checked,false);
  w.document.getElementById('saveDraft').click();
  await turn();
  assert.equal(posted.reviewed,false);
  assert.equal(posted.revision,1);
  w.eval("$('task').value='vectors';labelOptions();points=[{x:1,y:1},{x:8,y:1},{x:8,y:8},{x:1,y:8}];finish();");
  assert.equal(w.eval('instances.length'),1);
  assert.equal(w.eval('instances[0].category_id'),2);
  w.document.getElementById('undo').click();
  assert.equal(w.eval('instances.length'),0);
  console.log('PASS: rectangle, polygon, undo, layer isolation, review gate, draft/export payloads');
  w.close();
})().catch(error=>{console.error(error);w.close();process.exitCode=1});
