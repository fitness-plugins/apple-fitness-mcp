"""The dashboard's HTML/CSS/JS shell.

Kept in its own module purely for size: it is one self-contained document with
every style and script inlined, so the generated file works offline, from a
file:// URL, with no network access and no external assets. `dashboard.py`
substitutes the __DATA__ / __FIRST__ / __NOW__ / __NRUN__ / __NWK__ placeholders.

Do not add a CDN import or a <link> here — the whole point of this file is that
the output is a single portable artefact.
"""
from __future__ import annotations

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Maksim&#8217;s Training Progress</title>
<style>
:root{
  color-scheme: light;
  --surface-0:#f6f5f2; --surface-1:#fcfcfb; --surface-2:#efeeea;
  --line:#e2e0da; --line-strong:#cfccc3;
  --ink:#0b0b0b; --ink-2:#52514e; --ink-3:#83817a;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
  --good:#0ca30c; --warn:#fab219; --crit:#d03b3b;
  --shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px -12px rgba(0,0,0,.10);
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --surface-0:#111110; --surface-1:#1a1a19; --surface-2:#232322;
  --line:#302f2d; --line-strong:#403f3c;
  --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#8e8c83;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
  --good:#0ca30c; --warn:#fab219; --crit:#d03b3b;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -12px rgba(0,0,0,.6);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--surface-0); color:var(--ink);
  font:400 15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1120px;margin:0 auto;padding:28px 20px 80px}
header{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:22px;flex-wrap:wrap}
h1{font-size:26px;font-weight:650;letter-spacing:-.02em;margin:0 0 4px}
.sub{color:var(--ink-3);font-size:13.5px}
.themebtn{background:var(--surface-1);border:1px solid var(--line);color:var(--ink-2);
  border-radius:99px;padding:7px 14px;font-size:13px;cursor:pointer;font-family:inherit}
.themebtn:hover{border-color:var(--line-strong);color:var(--ink)}

nav{display:flex;gap:4px;background:var(--surface-2);padding:4px;border-radius:12px;
  margin-bottom:24px;width:fit-content;flex-wrap:wrap}
nav button{background:none;border:0;color:var(--ink-2);font:inherit;font-size:14px;
  padding:8px 18px;border-radius:9px;cursor:pointer;white-space:nowrap}
nav button[aria-selected=true]{background:var(--surface-1);color:var(--ink);font-weight:560;box-shadow:var(--shadow)}

.panel{display:none} .panel.on{display:block}

.lede{background:var(--surface-1);border:1px solid var(--line);border-radius:14px;
  padding:18px 20px;margin-bottom:20px;box-shadow:var(--shadow)}
.lede h2{font-size:15px;font-weight:620;margin:0 0 8px;letter-spacing:-.01em}
.lede p{margin:0 0 9px;color:var(--ink-2);font-size:14.5px}
.lede p:last-child{margin-bottom:0}
.lede b{color:var(--ink);font-weight:600}

.tiles{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));margin-bottom:20px}
.tile{background:var(--surface-1);border:1px solid var(--line);border-radius:14px;padding:15px 16px;box-shadow:var(--shadow)}
.tile .lab{font-size:12.5px;color:var(--ink-3);margin-bottom:6px;letter-spacing:.01em}
.tile .val{font-size:27px;font-weight:640;letter-spacing:-.025em;line-height:1.1}
.tile .val small{font-size:14px;font-weight:500;color:var(--ink-3);letter-spacing:0}
.tile .dlt{font-size:12.5px;margin-top:5px;color:var(--ink-3);display:flex;align-items:center;gap:5px}
.dot{width:7px;height:7px;border-radius:99px;flex:none}
.up{color:var(--good)} .dn{color:var(--crit)} .flat{color:var(--ink-3)}

.hero{background:var(--surface-1);border:1px solid var(--line);border-radius:14px;
  padding:22px 24px;margin-bottom:20px;box-shadow:var(--shadow);display:flex;gap:26px;align-items:center;flex-wrap:wrap}
.hero .n{font-size:52px;font-weight:660;letter-spacing:-.035em;line-height:1}
.hero .n small{font-size:20px;color:var(--ink-3);font-weight:500}
.hero .t{font-size:14.5px;color:var(--ink-2);max-width:520px}
.hero .t b{color:var(--ink)}

figure{margin:0 0 18px;background:var(--surface-1);border:1px solid var(--line);
  border-radius:14px;padding:18px 18px 12px;box-shadow:var(--shadow)}
figcaption{margin-bottom:14px}
figcaption .ft{font-size:14.5px;font-weight:600;letter-spacing:-.01em}
figcaption .fs{font-size:13px;color:var(--ink-3);margin-top:3px}
.grid2{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(min(430px,100%),1fr))}
.grid2 figure{margin-bottom:0}

.legend{display:flex;gap:16px;flex-wrap:wrap;margin:2px 0 12px;font-size:12.5px;color:var(--ink-2)}
.legend span{display:flex;align-items:center;gap:6px}
.key{width:14px;height:3px;border-radius:2px;flex:none}
.keyd{width:9px;height:9px;border-radius:99px;flex:none}
.seg{display:flex;gap:4px;margin:2px 0 10px}
.seg button{background:none;border:1px solid var(--line);color:var(--ink-2);font:inherit;
  font-size:12.5px;padding:3px 11px;border-radius:99px;cursor:pointer}
.seg button:hover{border-color:var(--line-strong);color:var(--ink)}
.seg button[aria-selected=true]{background:var(--surface-2);color:var(--ink);
  border-color:var(--line-strong);font-weight:560}

svg{display:block;width:100%;height:auto;overflow:visible}
.gl{stroke:var(--line);stroke-width:1}
.ax{fill:var(--ink-3);font-size:11px}
.dl{fill:var(--ink);font-size:11.5px;font-weight:600}

.tip{position:fixed;pointer-events:none;background:var(--surface-1);border:1px solid var(--line-strong);
  border-radius:9px;padding:8px 11px;font-size:12.5px;box-shadow:0 6px 20px -6px rgba(0,0,0,.28);
  opacity:0;transition:opacity .09s;z-index:60;white-space:nowrap;color:var(--ink)}
.tip.on{opacity:1}
.tip .tt{color:var(--ink-3);font-size:11.5px;margin-bottom:3px}
.tip .tr{display:flex;align-items:center;gap:7px;margin-top:2px}
.tip b{font-variant-numeric:tabular-nums}

details{background:var(--surface-1);border:1px solid var(--line);border-radius:14px;
  padding:14px 18px;margin-bottom:18px;box-shadow:var(--shadow)}
summary{cursor:pointer;font-size:14px;font-weight:560;color:var(--ink-2)}
summary:hover{color:var(--ink)}
details[open] summary{margin-bottom:12px}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th{text-align:right;color:var(--ink-3);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line);font-size:12px}
th:first-child,td:first-child{text-align:left}
td{padding:6px 8px;border-bottom:1px solid var(--line);color:var(--ink-2)}
tbody tr:hover td{background:var(--surface-2)}
.scroll{max-height:340px;overflow:auto}
.board{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
.board th{text-align:right;font-size:11.5px;color:var(--ink-3);font-weight:500;padding:7px 9px;
  border-bottom:1px solid var(--line-strong)}
.board th:first-child,.board td:first-child{text-align:left}
.board td{padding:8px 9px;border-bottom:1px solid var(--line);color:var(--ink-2)}
.board tr.now td{color:var(--ink);font-weight:600}
.board td.zero{color:var(--ink-3)}
.bar{display:inline-block;height:7px;border-radius:2px;background:var(--s1);vertical-align:middle;margin-right:7px}
.moveg{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));margin-bottom:20px}
.move{background:var(--surface-1);border:1px solid var(--line);border-radius:14px;padding:15px 16px;box-shadow:var(--shadow)}
.move .lab{font-size:12.5px;color:var(--ink-3);margin-bottom:8px}
.move .row{display:flex;align-items:baseline;gap:9px}
.move .from{font-size:16px;color:var(--ink-3);font-variant-numeric:tabular-nums}
.move .arw{color:var(--ink-3);font-size:13px}
.move .to{font-size:26px;font-weight:640;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.move .chg{font-size:12.5px;margin-top:6px;display:flex;align-items:center;gap:6px}
.move .u{font-size:13px;color:var(--ink-3);font-weight:500}
.warnbox{background:var(--surface-1);border:1px solid var(--line);border-left:3px solid var(--warn);
  border-radius:14px;padding:14px 18px;margin-bottom:20px;box-shadow:var(--shadow);
  font-size:13.5px;color:var(--ink-2)}
.warnbox b{color:var(--ink)}
ul.notes{margin:0;padding-left:18px;color:var(--ink-2);font-size:13.5px}
ul.notes li{margin-bottom:6px}
ul.notes b{color:var(--ink)}
@media (max-width:620px){.hero .n{font-size:40px}h1{font-size:22px}.wrap{padding:20px 14px 60px}
table.board{display:block;overflow-x:auto;white-space:nowrap}}
</style></head><body>
<div class="wrap">
<header>
  <div><h1>Training progress</h1>
  <div class="sub">Apple Health · __FIRST__ → __NOW__ · __NRUN__ runs analysed · __NWK__ weeks tracked</div></div>
  <button class="themebtn" id="tbtn">Dark</button>
</header>

<nav role="tablist">
  <button role="tab" aria-selected="true" data-p="month">Last 30 days</button>
  <button role="tab" aria-selected="false" data-p="perf">Running</button>
  <button role="tab" aria-selected="false" data-p="health">The engine</button>
  <button role="tab" aria-selected="false" data-p="recov">Load &amp; recovery</button>
  <button role="tab" aria-selected="false" data-p="about">Method</button>
</nav>

<section class="panel on" id="month"></section>
<section class="panel" id="perf"></section>
<section class="panel" id="health"></section>
<section class="panel" id="recov"></section>
<section class="panel" id="about"></section>
</div>
<div class="tip" id="tip"></div>
<script>
const D = __DATA__;
const K = D.kpi;
</script>
<script>
/* ---------- tiny chart engine (SVG, no deps) ---------- */
const NS='http://www.w3.org/2000/svg';
const tip=document.getElementById('tip');
const el=(t,a={})=>{const n=document.createElementNS(NS,t);for(const k in a)n.setAttribute(k,a[k]);return n;};
const dnum=s=>{const[y,m,d]=s.split('-').map(Number);return Date.UTC(y,m-1,d)/864e5;};
const dlab=v=>{const t=new Date(v*864e5);return t.toLocaleDateString('en',{month:'short',year:'2-digit',timeZone:'UTC'});};
const dfull=v=>{const t=new Date(v*864e5);return t.toLocaleDateString('en',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'});};
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const nice=(lo,hi,n=5)=>{const r=hi-lo||1,s=Math.pow(10,Math.floor(Math.log10(r/n)));
  const c=[1,2,2.5,5,10].map(x=>x*s).find(x=>r/x<=n+.5)||10*s;
  return{lo:Math.floor(lo/c)*c,hi:Math.ceil(hi/c)*c,st:c};};
/* Date-aware axis ticks. `nice` is built for linear numeric axes; applied to
   day-numbers it produces meaningless steps (a 870-day span gave 200-day ticks,
   leaving the last 98 days of the chart unlabelled). Dates get their own
   generator. mode 'month' reproduces the month-aligned behaviour timeChart has
   always used; 'auto' drops to Monday-aligned weekly ticks with day-precision
   labels once the window is short enough that month labels say nothing. */
/* day-first on purpose: dlab renders August 2026 as 'Aug 26', so a day label must not */
const dsh=v=>{const t=new Date(v*864e5);return t.getUTCDate()+' '+t.toLocaleDateString('en',{month:'short',timeZone:'UTC'});};
const dateTicks=(x0,x1,pw,div=70,mode='auto')=>{
  const maxT=Math.max(2,Math.floor(pw/div)),span=(x1-x0)||1;
  if(mode==='auto'&&span<=200){
    const st=[7,14,28,56].find(s=>span/s<=maxT)||56,out=[];
    for(let v=Math.ceil((x0-4)/st)*st+4;v<=x1;v+=st)if(v>=x0)out.push(v);
    return{ticks:out,fmt:dsh};}
  const months=span/30.4,stepM=[1,2,3,4,6,12].find(m=>months/m<=maxT)||12;
  const out=[],seen=new Set();
  let d=new Date(x0*864e5);d=new Date(Date.UTC(d.getUTCFullYear(),d.getUTCMonth(),1));
  for(;d.getTime()/864e5<=x1;d.setUTCMonth(d.getUTCMonth()+1)){
    const dv=d.getTime()/864e5;if(dv<x0)continue;if(d.getUTCMonth()%stepM)continue;
    const lb=dlab(dv);if(seen.has(lb))continue;seen.add(lb);out.push(dv);}
  return{ticks:out,fmt:dlab};};

function showTip(e,html){tip.innerHTML=html;tip.classList.add('on');
  const r=tip.getBoundingClientRect();let x=e.clientX+14,y=e.clientY-10;
  if(x+r.width>innerWidth-8)x=e.clientX-r.width-14;
  if(y+r.height>innerHeight-8)y=innerHeight-r.height-8;
  tip.style.left=x+'px';tip.style.top=Math.max(8,y)+'px';}
const hideTip=()=>tip.classList.remove('on');

/* generic time chart: series = [{name,color,pts,type:'line'|'col'|'dot',fill,r,unit,dec}] */
function timeChart(host,series,opt={}){
  const W=opt.w||920,H=opt.h||230,P={t:14,r:opt.pr??54,b:26,l:opt.pl??44};
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,preserveAspectRatio:'none',role:'img'});
  svg.style.height=H+'px';svg.setAttribute('preserveAspectRatio','xMidYMid meet');
  /* opt.x0/x1 moved the scale but never the data: 81 pre-window points per
     series were still stroked, off the left edge and over the y-tick labels. */
  if(opt.x0!==undefined||opt.x1!==undefined){
    const lo=opt.x0!==undefined?dnum(opt.x0):-Infinity;
    const hi=opt.x1!==undefined?dnum(opt.x1):Infinity;
    series=series.map(s=>Object.assign({},s,
      {pts:s.pts.filter(p=>{const v=dnum(p[0]);return v>=lo&&v<=hi;})}))
      .filter(s=>s.pts.length);}
  const all=series.flatMap(s=>s.pts);
  if(!all.length)return;
  const xs=all.map(p=>dnum(p[0]));
  /* The y-extent must cover every point that will be DRAWN. Taking it from the
     trend series alone let the dot branch below silently delete 30 of 69 nightly
     HRV readings — including every value above 55, i.e. the evidence for the
     caption. opt.domain is kept for call-site compatibility and ignored. */
  const ys=all.map(p=>p[1]);
  let x0=opt.x0!==undefined?dnum(opt.x0):Math.min(...xs),x1=Math.max(...xs);
  let ylo=opt.ylo!==undefined?opt.ylo:Math.min(...ys),yhi=opt.yhi!==undefined?opt.yhi:Math.max(...ys);
  if(opt.zero)ylo=0;
  const pad=(yhi-ylo)*0.12||1; if(opt.ylo===undefined)ylo-=pad; if(opt.yhi===undefined)yhi+=pad;
  if(opt.zero)ylo=0;
  const nz=nice(ylo,yhi,opt.ticks||4); if(opt.ylo===undefined)ylo=nz.lo; if(opt.yhi===undefined)yhi=nz.hi;
  const X=v=>P.l+(v-x0)/((x1-x0)||1)*(W-P.l-P.r);
  const Y=v=>H-P.b-(v-ylo)/((yhi-ylo)||1)*(H-P.t-P.b);
  /* gridlines + y ticks */
  for(let v=nz.lo;v<=yhi+1e-9;v+=nz.st){ if(v<ylo-1e-9)continue;
    svg.appendChild(el('line',{x1:P.l,x2:W-P.r,y1:Y(v),y2:Y(v),class:'gl'}));
    const t=el('text',{x:P.l-8,y:Y(v)+4,class:'ax','text-anchor':'end'});
    t.textContent=opt.yfmt?opt.yfmt(v):(Math.round(v*100)/100).toLocaleString();
    svg.appendChild(t);}
  /* x ticks */
  const xt=dateTicks(x0,x1,W-P.l-P.r,62,opt.xmode||'auto');
  xt.ticks.forEach(dv=>{
    const t=el('text',{x:X(dv),y:H-8,class:'ax','text-anchor':'middle'});
    t.textContent=xt.fmt(dv);svg.appendChild(t);});
  /* bands */
  (opt.bands||[]).forEach(b=>{
    const xa=X(dnum(b.from)),xb=X(dnum(b.to));
    svg.appendChild(el('rect',{x:xa,y:P.t,width:Math.max(1,xb-xa),height:H-P.t-P.b,
      fill:css(b.color||'--s3'),opacity:b.op??0.07}));
    if(b.label){const t=el('text',{x:xa+6,y:P.t+13,class:'ax'});t.textContent=b.label;
      t.setAttribute('fill',css('--ink-3'));svg.appendChild(t);}});
  /* zero line */
  if(opt.zeroLine!==undefined&&ylo<opt.zeroLine&&yhi>opt.zeroLine)
    svg.appendChild(el('line',{x1:P.l,x2:W-P.r,y1:Y(opt.zeroLine),y2:Y(opt.zeroLine),
      stroke:css('--line-strong'),'stroke-width':1}));
  /* marks */
  const surf=css('--surface-1');
  series.forEach(s=>{
    const c=css(s.color);
    if(s.type==='col'){
      const bw=Math.max(1.5,Math.min(24,(W-P.l-P.r)/(s.pts.length*1.35)));
      s.pts.forEach(p=>{ if(!p[1])return;
        const h=Math.max(1.5,Y(0)-Y(p[1]));
        svg.appendChild(el('rect',{x:X(dnum(p[0]))-bw/2,y:Y(p[1]),width:bw,height:h,
          fill:c,rx:Math.min(3,bw/2),ry:Math.min(3,bw/2)}));});
    } else if(s.type==='dot'){
      s.pts.forEach(p=>{ if(p[1]<ylo||p[1]>yhi)return;
        svg.appendChild(el('circle',{cx:X(dnum(p[0])),cy:Y(p[1]),r:s.r||4,fill:c,
          stroke:surf,'stroke-width':1.5,opacity:s.op??1}));});
    } else {
      if(s.fill){const a=[`M${X(dnum(s.pts[0][0]))},${Y(ylo)}`];
        s.pts.forEach(p=>a.push(`L${X(dnum(p[0]))},${Y(p[1])}`));
        a.push(`L${X(dnum(s.pts[s.pts.length-1][0]))},${Y(ylo)}Z`);
        svg.appendChild(el('path',{d:a.join(''),fill:c,opacity:.10}));}
      const gapD=s.gap||0; let prev=null;
      const d2=s.pts.map((p,i)=>{const cur=dnum(p[0]);
        const brk=i===0||(gapD&&prev!==null&&cur-prev>gapD); prev=cur;
        return (brk?'M':'L')+X(cur)+','+Y(p[1]);}).join('');
      svg.appendChild(el('path',{d:d2,fill:'none',stroke:c,'stroke-width':s.w||2,
        'stroke-linejoin':'round','stroke-linecap':'round',opacity:s.op??1}));
      const lp=s.pts[s.pts.length-1];
      if(s.endDot!==false){
        svg.appendChild(el('circle',{cx:X(dnum(lp[0])),cy:Y(lp[1]),r:4.5,fill:c,stroke:surf,'stroke-width':2}));}
      if(s.endLabel!==false){
        const t=el('text',{x:X(dnum(lp[0]))+9,y:Y(lp[1])+4,class:'dl'});
        t.textContent=(s.fmt?s.fmt(lp[1]):lp[1])+(s.unit||'');svg.appendChild(t);}
    }});
  /* crosshair + tooltip */
  const cross=el('line',{x1:0,x2:0,y1:P.t,y2:H-P.b,stroke:css('--line-strong'),'stroke-width':1,opacity:0});
  svg.appendChild(cross);
  const marks=series.map(s=>{const c=el('circle',{r:5,fill:css(s.color),stroke:surf,'stroke-width':2,opacity:0});
    svg.appendChild(c);return c;});
  const hit=el('rect',{x:P.l,y:0,width:W-P.l-P.r,height:H,fill:'transparent'});
  svg.appendChild(hit);
  hit.addEventListener('pointermove',e=>{
    const b=svg.getBoundingClientRect();
    const px=(e.clientX-b.left)/b.width*W, dv=x0+(px-P.l)/(W-P.l-P.r)*(x1-x0);
    let rows='',shown=null;
    series.forEach((s,i)=>{
      let best=null,bd=1e9;
      s.pts.forEach(p=>{const dd=Math.abs(dnum(p[0])-dv);if(dd<bd){bd=dd;best=p;}});
      if(!best||bd>(opt.snap||14)){marks[i].setAttribute('opacity',0);return;}
      shown=shown||best;
      marks[i].setAttribute('cx',X(dnum(best[0])));marks[i].setAttribute('cy',Y(best[1]));
      marks[i].setAttribute('opacity',1);
      rows+=`<div class="tr"><span class="dot" style="background:${css(s.color)}"></span>`+
            `${s.name} <b>${s.fmt?s.fmt(best[1]):best[1]}${s.unit||''}</b></div>`;});
    if(!shown){cross.setAttribute('opacity',0);hideTip();return;}
    cross.setAttribute('x1',X(dnum(shown[0])));cross.setAttribute('x2',X(dnum(shown[0])));
    cross.setAttribute('opacity',.7);
    showTip(e,`<div class="tt">${dfull(dnum(shown[0]))}</div>${rows}`);});
  hit.addEventListener('pointerleave',()=>{cross.setAttribute('opacity',0);
    marks.forEach(m=>m.setAttribute('opacity',0));hideTip();});
  host.appendChild(svg);
}

/* scatter with categorical color */
/* opt.views = [{label,from,to}] renders a domain switcher above the plot */
function scatter(host,pts,opt={}){
  const vs=opt.views;
  if(!vs||vs.length<2){drawScatter(host,pts,opt);return;}
  const bar=document.createElement('div');bar.className='seg';
  const box=document.createElement('div');
  let cur=opt.view??0;
  const draw=()=>{box.innerHTML='';const v=vs[cur];
    drawScatter(box,pts,Object.assign({},opt,{views:null,
      xmin:v.from!==undefined?dnum(v.from):undefined,
      xmax:v.to!==undefined?dnum(v.to):undefined}));};
  vs.forEach((v,i)=>{const b=document.createElement('button');b.type='button';
    b.textContent=v.label;b.setAttribute('aria-selected',String(i===cur));
    b.addEventListener('click',()=>{cur=i;
      [...bar.children].forEach((c,j)=>c.setAttribute('aria-selected',String(j===cur)));
      draw();});
    bar.appendChild(b);});
  host.appendChild(bar);host.appendChild(box);draw();
}

function drawScatter(host,pts,opt={}){
  const W=opt.w||920,H=opt.h||260,P={t:14,r:opt.pr??18,b:30,l:opt.pl||48};
  if(opt.xmin!==undefined)pts=pts.filter(p=>p.x>=opt.xmin);
  if(opt.xmax!==undefined)pts=pts.filter(p=>p.x<=opt.xmax);
  if(!pts.length){const n=document.createElement('div');n.className='fs';
    n.textContent='No runs in this window.';host.appendChild(n);return;}
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`});svg.setAttribute('preserveAspectRatio','xMidYMid meet');
  const xs=pts.map(p=>p.x),ys=pts.map(p=>p.y);
  const nx=nice(Math.min(...xs),Math.max(...xs),5), ny=nice(Math.min(...ys),Math.max(...ys),4);
  let x0=opt.x0??(opt.xclamp?Math.min(...xs):nx.lo),x1=opt.x1??(opt.xclamp?Math.max(...xs):nx.hi);
  /* widen the domain by the largest dot radius so edge points are not clipped */
  if(opt.xpad!==false){const rmax=Math.max(...pts.map(p=>p.r||5.5));
    const xp=((x1-x0)||1)*(rmax+2)/Math.max(1,W-P.l-P.r);x0-=xp;x1+=xp;}
  const y0=opt.y0??ny.lo,y1=opt.y1??ny.hi;
  const X=v=>P.l+(v-x0)/((x1-x0)||1)*(W-P.l-P.r);
  const Y=v=>opt.flipY?P.t+(v-y0)/((y1-y0)||1)*(H-P.t-P.b):H-P.b-(v-y0)/((y1-y0)||1)*(H-P.t-P.b);
  for(let v=ny.lo;v<=y1+1e-9;v+=ny.st){if(v<y0-1e-9)continue;
    svg.appendChild(el('line',{x1:P.l,x2:W-P.r,y1:Y(v),y2:Y(v),class:'gl'}));
    const t=el('text',{x:P.l-8,y:Y(v)+4,class:'ax','text-anchor':'end'});
    t.textContent=opt.yfmt?opt.yfmt(v):v;svg.appendChild(t);}
  const xt=opt.xdate?dateTicks(x0,x1,W-P.l-P.r)
    :{ticks:(()=>{const a=[];for(let v=nx.lo;v<=x1+1e-9;v+=nx.st)if(v>=x0-1e-9)a.push(v);
      return a;})(),fmt:v=>v};
  xt.ticks.forEach(v=>{
    const t=el('text',{x:X(v),y:H-9,class:'ax',
      'text-anchor':X(v)<P.l+20?'start':X(v)>W-P.r-20?'end':'middle'});
    t.textContent=opt.xfmt?opt.xfmt(v):xt.fmt(v);svg.appendChild(t);});
  if(opt.xlab){const t=el('text',{x:(P.l+W-P.r)/2,y:H+4,class:'ax','text-anchor':'middle'});
    t.textContent=opt.xlab;svg.appendChild(t);}
  const surf=css('--surface-1');
  pts.forEach(p=>{
    const c=el('circle',{cx:X(p.x),cy:Y(p.y),r:p.r||5.5,fill:css(p.color),
      stroke:surf,'stroke-width':2,opacity:.95,style:'cursor:crosshair'});
    c.addEventListener('pointerenter',e=>showTip(e,p.tip));
    c.addEventListener('pointermove',e=>showTip(e,p.tip));
    c.addEventListener('pointerleave',hideTip);
    svg.appendChild(c);});
  host.appendChild(svg);
}

/* ---------- helpers for building blocks ---------- */
const h=(s)=>{const d=document.createElement('div');d.innerHTML=s.trim();return d.firstChild;};
function figure(parent,title,sub,legend){
  const f=document.createElement('figure');
  f.innerHTML=`<figcaption><div class="ft">${title}</div>${sub?`<div class="fs">${sub}</div>`:''}</figcaption>`
    +(legend?`<div class="legend">${legend}</div>`:'');
  parent.appendChild(f);return f;}
const keyLine=(c,n)=>`<span><i class="key" style="background:${c}"></i>${n}</span>`;
const keyDot =(c,n)=>`<span><i class="keyd" style="background:${c}"></i>${n}</span>`;
function tiles(parent,list){
  const g=document.createElement('div');g.className='tiles';
  g.innerHTML=list.map(t=>`<div class="tile"><div class="lab">${t.l}</div>
    <div class="val">${t.v}${t.u?`<small> ${t.u}</small>`:''}</div>
    ${t.d?`<div class="dlt ${t.c||'flat'}">${t.d}</div>`:''}</div>`).join('');
  parent.appendChild(g);}
const nfmt=(v,d=1)=>Number(v).toFixed(d).replace(/\.0+$/,'');
const paceFmt=v=>{const m=Math.floor(v),s=Math.round((v-m)*60);return `${m}:${String(s).padStart(2,'0')}`;};
const dstr=s=>s?dfull(dnum(s)):'';

/* Prose for a narrative block. `D.txt` is written by a separate reasoning pass
   and only reaches here when it was written against this exact data date —
   dashboard.py drops a stale one. Every block therefore still carries a
   fallback that writes itself from the numbers, so the document is never
   waiting on anything to be readable. */
const TXT=(D.txt&&D.txt.blocks)||{};
function lede(key,fallback){
  const b=TXT[key];
  if(!b||!Array.isArray(b.p)||!b.p.length)return fallback();
  return `<div class="lede">`+(b.h2?`<h2>${b.h2}</h2>`:'')+
    b.p.map(x=>`<p>${x}</p>`).join('')+
    (Array.isArray(b.li)&&b.li.length
      ?`<ul class="notes">${b.li.map(x=>`<li>${x}</li>`).join('')}</ul>`:'')+
    `</div>`;
}
/* One-paragraph blocks (the tab heroes) take just the text. */
function ledeText(key,fallback){
  const b=TXT[key];
  return b&&Array.isArray(b.p)&&b.p.length?b.p.join(' '):fallback();
}
const clockFmt=v=>{const m=Math.floor(v),s=Math.round((v-m)*60);return `${m}:${String(s).padStart(2,'0')}`;};
</script>
<script>
/* =================== PANEL 0 — LAST 30 DAYS =================== */

/* Which of the four core signals actually moved this window, in words. Written
   from the data rather than fixed prose, so it cannot go stale on a rebuild. */
function movesSummary(){
  const M=D.month;
  const dir=(m,goodDown)=>{if(!m)return null;if(Math.abs(m.d)<0.05)return 'flat';
    return (goodDown?m.d<0:m.d>0)?'up':'down';};
  const up=[],flat=[],down=[];
  [['resting heart rate',M.rhr,true],['HRV',M.hrv,false],
   ['sleep',M.sleep,false],['daily steps',M.steps,false]].forEach(([n,m,gd])=>{
    const d=dir(m,gd); if(!d)return; (d==='up'?up:d==='flat'?flat:down).push(n);});
  const list=a=>a.length===1?a[0]:a.slice(0,-1).join(', ')+' and '+a[a.length-1];
  const bits=[];
  if(up.length)bits.push(`<b>${list(up)}</b> improved`);
  if(flat.length)bits.push(`<b>${list(flat)}</b> held flat`);
  if(down.length)bits.push(`<b>${list(down)}</b> slipped`);
  if(!bits.length)return '';
  /* the first word may sit inside a tag, so capitalise the first letter, not the first char */
  return (bits.join('; ')+'.').replace(/^((?:<[^>]+>)*)([a-z])/,(m,t,c)=>t+c.toUpperCase());
}

/* The plan save_weekly_plan last wrote, rendered as-is. */
function buildPlan(P){
  const pl=D.plan; if(!pl||!pl.planned||!pl.planned.length)return;
  const esc=t=>String(t).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  const nl=t=>esc(t).replace(/\n/g,'<br>');
  const items=pl.planned.map(x=>`<li><b>${nl(x.title||x.id)}</b>`+
    (x.notes?`<div style="margin-top:5px;color:var(--ink-2)">${nl(x.notes)}</div>`:'')+
    `</li>`).join('');
  P.appendChild(h(`<div class="lede"><h2>The plan on file`+
    (pl.week_of?` — week of ${dstr(pl.week_of)}`:'')+`</h2>`+
    (pl.title?`<p><b>${nl(pl.title)}</b></p>`:'')+
    (pl.notes?`<p>${nl(pl.notes)}</p>`:'')+
    `<ul class="notes">${items}</ul></div>`));
}

function buildMonth(){
const P=document.getElementById('month');
const M=D.month, S1=css('--s1'),S2=css('--s2'),S3=css('--s3');
const GOOD=css('--good'),CRIT=css('--crit');
/* The last 90 days, measured from the data. As a frozen literal this window had
   already grown to 105 days under three titles still reading "last 90 days". */
const W=new Date((dnum(D.generated)-90)*864e5).toISOString().slice(0,10);
const clip=a=>a.filter(p=>p[0]>=W);
const hrrGainN=M.hrr_b-M.hrr_a,hrrGain=Math.abs(hrrGainN).toFixed(1);

P.appendChild(h(`<div class="hero">
  <div class="n">${hrrGainN<0?'−':'+'}${hrrGain}<small> bpm</small></div>
  <div class="t">${ledeText('month.hero',()=>`${hrrGainN<0?'loss':'gain'} in <b>2-minute heart-rate recovery</b> since this
  block started — ${M.hrr_a} on ${dstr(M.hrr_a_d)}, <b>${M.hrr_b}</b> on ${dstr(M.hrr_b_d)}, across all
  ${M.hrr_n} measurements in between. Of everything in your data this is the clearest evidence
  that the last month of training is working.`)}</div></div>`));

buildPlan(P);

/* movement cards: first half vs second half of the last 30 days */
const nf=(v,d)=>Number(v.toFixed(d??1)).toLocaleString('en',{minimumFractionDigits:d??1,maximumFractionDigits:d??1});
function move(lab,m,unit,goodDown,dec){
  if(!m)return '';
  const flat=Math.abs(m.d)<0.05;
  const good=goodDown?m.d<0:m.d>0;
  const arrow=flat?'—':m.d>0?'▲':'▼';
  return `<div class="move"><div class="lab">${lab}</div>
    <div class="row"><span class="from">${nf(m.a,dec)}</span><span class="arw">→</span>
    <span class="to">${nf(m.b,dec)}</span><span class="u">${unit}</span></div>
    <div class="chg" style="color:${flat?'var(--ink-3)':good?GOOD:CRIT}">${arrow} ${m.d>0?'+':''}${nf(m.d,dec)} ${unit}
    <span style="color:var(--ink-3)">across the month</span></div></div>`;}

P.appendChild(h(lede('month.stand',()=>`<div class="lede"><h2>Where you stand, ${dstr(M.w0)} → ${dstr(M.w1)}</h2>
<p>Every number below compares the <b>first half of the last 30 days with the second half</b>,
so it shows movement <em>inside</em> the month rather than against a baseline.
${movesSummary()}</p>
<p>You ran <b>${M.n30} times for ${M.km30} km</b> in the window, and have now put together
${K.weeks_run_q3} running weeks out of ${K.weeks_q3} — by a wide margin your most consistent
stretch on record.</p></div>`)));

P.appendChild(h(`<div class="moveg">
  ${move('Resting heart rate',M.rhr,'bpm',true)}
  ${move('HRV (overnight)',M.hrv,'ms',false)}
  ${move('Sleep',M.sleep,'h',false)}
  ${move('Daily steps',M.steps,'',false,0)}
</div>`));

P.appendChild(h(`<div class="warnbox"><b>Why there is no "vs last month" comparison.</b>
Your watch recorded almost nothing in June — one resting-HR reading and no sleep for the whole
month — so a straight month-over-month delta would be measuring the gap in the data, not a change
in you. The within-month comparison above is the honest substitute.</div>`));

/* week-by-week scoreboard */
{const f=figure(P,'The last 8 weeks, week by week',
  'Everything that matters on one row. The bar is running volume.');
 const mx=Math.max(...M.board.map(b=>b.km))||1;
 const wl=w=>{const d=new Date(dnum(w)*864e5);
   return d.toLocaleDateString('en',{day:'numeric',month:'short',timeZone:'UTC'});};
 f.appendChild(h(`<table class="board"><thead><tr>
   <th>Week of</th><th>Distance</th><th>Runs</th><th>Avg pace</th><th>Avg run HR</th>
   <th>Load</th><th>Resting HR</th><th>HRV</th><th>Sleep</th></tr></thead><tbody>`+
   M.board.map((b,i)=>`<tr class="${i===M.board.length-1?'now':''}">
     <td>${wl(b.w)}${i===M.board.length-1?' <span style="color:var(--ink-3);font-weight:400">(in progress)</span>':''}</td>
     <td class="${b.km?'':'zero'}">${b.km?`<i class="bar" style="width:${Math.max(3,b.km/mx*54)}px"></i>${b.km} km`:'—'}</td>
     <td class="${b.n?'':'zero'}">${b.n||'—'}</td>
     <td class="${b.pace?'':'zero'}">${b.pace?paceFmt(b.pace):'—'}</td>
     <td class="${b.hr?'':'zero'}">${b.hr?b.hr+' bpm':'—'}</td>
     <td class="${b.load?'':'zero'}">${b.load||'—'}</td>
     <td>${b.rhr?b.rhr.toFixed(1):'—'}</td>
     <td>${b.hrv?b.hrv.toFixed(1):'—'}</td>
     <td>${b.sleep?b.sleep.toFixed(1)+' h':'—'}</td></tr>`).join('')
   +`</tbody></table>`));}

/* zoomed 90-day charts */
const g=document.createElement('div');g.className='grid2';P.appendChild(g);

{const f=figure(g,'Resting heart rate — last 90 days',
  'The break is June, when the watch was not worn. Since the block began the trend has been steadily down.',
  keyDot(S1,'Daily')+keyLine(S2,'14-day trend'));
 timeChart(f,[
  {name:'Daily',color:'--s1',type:'dot',pts:clip(D.rhr_raw),r:3.2,op:.45},
  {name:'Trend',color:'--s2',pts:clip(D.rhr),gap:20,fmt:v=>nfmt(v)}],
  {h:230,w:490,ticks:4,domain:[1],pr:40,pl:36});}

{const f=figure(g,'HRV — last 90 days','Overnight heart-rate variability. Trending up through the block.',
  keyDot(S3,'Nightly')+keyLine(S1,'14-day trend'));
 timeChart(f,[
  {name:'Nightly',color:'--s3',type:'dot',pts:clip(D.hrv_raw),r:3.2,op:.45},
  {name:'Trend',color:'--s1',pts:clip(D.hrv),gap:20,fmt:v=>nfmt(v)}],
  {h:230,w:490,ticks:4,domain:[1],pr:40,pl:36});}

{const hp=clip(D.hrr),hd=hp.length>1?hp[hp.length-1][1]-hp[0][1]:0;
 const f=figure(g,'Heart-rate recovery — every reading this block',
  `How far your heart rate falls in the 2 minutes after a hard effort. `+
  `${hp.length} reading${hp.length===1?'':'s'}`+
  (hp.length<2?'.':hd>0.5?`, ${nfmt(hd)} bpm better across the block.`
    :hd<-0.5?`, ${nfmt(-hd)} bpm worse across the block.`:', flat across the block.'),
  keyDot(S3,'One reading'));
 timeChart(f,[
  {name:'HR recovery',color:'--s3',pts:hp,fmt:v=>nfmt(v)},
  {name:'Reading',color:'--s3',type:'dot',pts:hp,r:4,op:.55,endLabel:false,endDot:false}],
  {h:230,w:490,ticks:4,snap:30,pr:40,pl:36,domain:[0]});}

{const sd=M.sleep?M.sleep.d:0;
 const f=figure(g,'Sleep — last 90 days',
  sd<-0.15?'The one signal moving the wrong way this month.'
  :sd>0.15?`Up ${nfmt(sd)} h across the month — duration is not the open question, bedtime is.`
  :'Holding flat across the month.',
  keyDot(S1,'Nightly')+keyLine(S2,'14-day trend'));
 timeChart(f,[
  {name:'Nightly',color:'--s1',type:'dot',pts:clip(D.sleep_raw),r:3.2,op:.45},
  {name:'Trend',color:'--s2',pts:clip(D.sleep),gap:20,fmt:v=>nfmt(v),unit:' h'}],
  {h:230,w:490,ticks:4,domain:[1],pr:48,pl:36});}

/* runs this block */
{const f=figure(P,'Every run in this block',
  'Colour is the heart-rate zone. Effort has stayed mostly easy while distance held around 10 km.',
  keyDot(S3,'Recovery / easy ≤165')+keyDot(S1,'Grey 166–177')+keyDot(S2,'Threshold+ 178+'));
 f.appendChild(h(`<table class="board"><thead><tr><th>Date</th><th>Distance</th><th>Time</th>
   <th>Pace</th><th>Avg HR</th><th>Zone</th><th>Efficiency</th></tr></thead><tbody>`+
   M.runs.slice().reverse().map(r=>{
     const c=r.zone<=2?S3:r.zone===3?S1:S2;
     const d=new Date(dnum(r.d)*864e5).toLocaleDateString('en',{weekday:'short',day:'numeric',month:'short',timeZone:'UTC'});
     return `<tr><td><span class="dot" style="background:${c};display:inline-block;margin-right:8px"></span>${d}</td>
       <td>${r.km} km</td><td>${Math.round(r.min)}′</td><td>${paceFmt(r.pace)}</td>
       <td>${r.hr} bpm</td><td>${['','Recovery','Easy','Grey','Threshold','VO₂max'][r.zone]}</td><td>${r.ei.toFixed(1)}</td></tr>`;}).join('')
   +`</tbody></table>`));}

const sleepNote=()=>{const x=M.sleep;
  if(!x)return 'no usable sleep data in this window.';
  if(x.d<-0.15)return `sleep fell from <b>${x.a} h to ${x.b} h</b> across the month while training load held. `+
    `If resting HR stalls or HRV turns over in the next fortnight, sleep is the first place to look.`;
  if(x.d>0.15)return `sleep is up from ${x.a} h to <b>${x.b} h</b>. Duration is not the problem — `+
    `bedtime is, and it has drifted from about 22:00 in 2024 to past 01:00 now. Nothing has broken yet, `+
    `but it is the one clear regression in the whole dataset.`;
  return `sleep is holding at <b>${x.b} h</b>. Duration is fine; bedtime is the part that has drifted `+
    `later since 2024, and it is the one clear regression in the data.`;};
const workNote=()=>{
  const r=M.rhr, moved=r&&Math.abs(r.d)>=0.2;
  const lead=!r?'':moved&&r.d<0?`resting HR is <b>down ${Math.abs(r.d).toFixed(1)} bpm</b> inside 30 days, `
    :moved?`resting HR is <b>up ${r.d.toFixed(1)} bpm</b> over the month, `
    :`resting HR is flat over the month, `;
  return lead+`and 2-minute heart-rate recovery has gone <b>${M.hrr_a} → ${M.hrr_b} bpm</b> across `+
    `${M.hrr_n} measurements since the block started. Heart-rate recovery is the cleanest signal you have: `+
    `unlike pace or the VO&#8322;max estimate it does not care how fast you chose to run.`;};
const nextNote=()=>{
  const r=K.weeks_run_q3, t=K.weeks_q3, open=t>r;
  return `you have <b>${r} straight weeks with a run</b>${open?` and week ${t} is still open`:''}. `+
    `Every previous block died at five or six. Reaching <b>10 unbroken weeks</b> would be new territory `+
    `and is worth more than any single fast session — the gaps, not the fitness, are what has always capped you.`;};

P.appendChild(h(lede('month.read',()=>`<div class="lede"><h2>The read on this month</h2>
<p><b>Working:</b> ${workNote()}</p>
<p><b>Watch:</b> ${sleepNote()}</p>
<p><b>Next:</b> ${nextNote()}</p></div>`)));
}
</script>
<script>
/* =================== PANEL 1 — RUNNING =================== */
function buildPerf(){
const P=document.getElementById('perf');
const S1=css('--s1'),S2=css('--s2'),S3=css('--s3');

P.appendChild(h(`<div class="hero">
  <div class="n">${K.weeks_run_q3}<small>/${K.weeks_q3}</small></div>
  <div class="t">weeks with a run since <b>${dstr(D.month.block_start)}</b>. Before this block you ran in
  <b>${K.weeks_run_total-K.weeks_run_q3} of ${K.weeks_total-K.weeks_q3}</b> tracked weeks
  (${Math.round((K.weeks_run_total-K.weeks_run_q3)/(K.weeks_total-K.weeks_q3)*100)}%).
  <b>Consistency — not speed — is the thing that has actually changed.</b></div></div>`));

P.appendChild(h(lede('perf.intro',()=>`<div class="lede"><h2>What the running data says</h2>
<p>Your history is <b>four short seasons separated by long gaps</b> — 75, 92, 204 and 220 days
without a run. Every time, you rebuilt from zero. The block that started ${dstr(D.month.runs[0].d)} is the
first one to string weeks together.</p>
<p>You also changed <b>how</b> you run. Average heart rate on a run fell from
<b>${K.hr_prior} to ${K.hr_block} bpm</b> while average distance rose from
<b>${K.km_run_prior} to ${K.km_run_block} km</b>. Pace looks slower
(${paceFmt(K.pace_prior)} → ${paceFmt(K.pace_block)} /km) — that is the intended trade, not a decline.
Previously almost every run sat in zone 4–5; now most sit in zone 2–3.</p>
<p>The payoff is already measurable: your <b>2-minute heart-rate recovery</b> has climbed
from ${K.hrr_blockstart} to <b>${K.hrr_now} bpm</b> inside this block alone. That is the single
cleanest read that the aerobic base is building.</p></div>`)));

tiles(P,[
 {l:'Weekly volume, this block',v:K.km_block,u:'km',d:`vs ${K.km_prior} km in prior active weeks`,c:'up'},
 {l:'Distance run in 2026',v:Math.round(K.km_2026),u:'km'},
 {l:'Longest run',v:K.longest.km,u:'km',d:`${K.longest.d} · ${paceFmt(K.longest.pace)}/km @ ${K.longest.hr} bpm`},
 {l:'Avg run HR, this block',v:K.hr_block,u:'bpm',d:`was ${K.hr_prior} bpm — easier by design`,c:'up'},
]);

/* ---- the sub-45 question, computed from the fastest 10 km on record ---- */
{const c=D.runs.filter(r=>r.km>=9.7&&r.km<=10.6).sort((a,b)=>a.pace-b.pace);
 if(c.length){
  const pb=c[0], t=pb.pace*10, need=4.5, gap=(1-need/pb.pace)*100;
  const effort=Math.round(pb.hr/D.hrmax*100);
  P.appendChild(h(lede('perf.tenk',()=>`<div class="lede"><h2>The sub-45 question</h2>
  <p>Your fastest 10 km on record is <b>${clockFmt(t)}</b> — ${dstr(pb.d)}, ${paceFmt(pb.pace)}/km at
  ${pb.hr} bpm average. That is <b>${effort}% of functional HRmax</b> held for the whole distance, so it
  was a flat-out effort and not a training run: there is no hidden margin in it.</p>
  <p><b>45:00 means holding 4:30/km</b>, which is <b>${gap.toFixed(1)}% faster</b> than that PB for
  three quarters of an hour. A well-executed eight-week threshold block moves threshold pace about
  3–5%. Two of them back to back get you roughly halfway there.</p>
  <p>The limit is not the sessions, it is <b>volume and unbroken weeks</b>. You average
  ${K.km_block} km a week in this block; runners who go under 45 usually sit at 40–60. Raising that,
  without breaking the streak, is the single lever that matters.</p>
  <ul class="notes">
   <li><b>Realistic by December</b> — 47:30–49:00, with 46:30 if everything lands.</li>
   <li><b>Realistic by February</b> — 46:00–47:30. <b>45:00 is the upper edge of luck</b>, and a
   Moscow winter costs 15–30 s/km on ice unless the effort moves indoors.</li>
   <li><b>Spring 2027</b> — 44:00–45:00 on a normal progression, which is where this target belongs.</li>
   <li>These are estimates from one PB and the current block, not a measurement. A fresh-legs 10 km
   time trial in late September replaces all of them with a real number.</li>
  </ul></div>`)));}}

/* weekly volume */
{const f=figure(P,'Weekly running volume',
  'Every week since your first tracked run. The empty stretches are the story.',
  keyLine(S1,'km run per week'));
 timeChart(f,[{name:'Volume',color:'--s1',type:'col',pts:D.weekly.map(w=>[w.w,w.km]),unit:' km'}],
   {h:210,zero:true,pr:20,yfmt:v=>v+' km',
    bands:[{from:D.month.block_start,to:D.generated,label:'current block',color:'--s3',op:.09}],snap:5});}

/* every run: pace over time coloured by intensity */
{const zc=z=>z<=2?'--s3':z===3?'--s1':'--s2';
 const zn=z=>z===1?'Recovery (≤149 bpm)':z===2?'Easy (150–165)':z===3?'Grey zone (166–177)'
   :z===4?'Threshold (178–186)':'VO₂max (187+)';
 const f=figure(P,'Every run — pace, and how hard it felt',
   'Dot size = distance. Higher on the chart is faster. Colour is the heart-rate zone the run averaged. Opens on the current block; switch to all time above.',
   keyDot(S3,'Recovery / easy ≤165')+keyDot(S1,'Grey 166–177')+keyDot(S2,'Threshold+ 178+'));
 scatter(f,D.runs.map(r=>({x:dnum(r.d),y:r.pace,color:zc(r.zone),
   r:3.5+Math.sqrt(r.km)*1.15,
   tip:`<div class="tt">${dfull(dnum(r.d))}</div>`+
       `<div class="tr"><span class="dot" style="background:${css(zc(r.zone))}"></span>${zn(r.zone)}</div>`+
       `<div class="tr">Distance <b>${r.km} km</b></div><div class="tr">Pace <b>${paceFmt(r.pace)} /km</b></div>`+
       `<div class="tr">Avg HR <b>${r.hr} bpm</b></div>`})),
   {h:250,flipY:true,yfmt:v=>paceFmt(v)+'/km',xdate:true,pl:56,xclamp:true,pr:30,
    y0:4.5,y1:8.5,
    views:[{label:'Current block',from:D.month.block_start},{label:'All time'}]});}

/* pace vs HR */
{const era=d=>d<'2024-11'?0:d<'2026-06'?1:2;
 const ec=['--s3','--s1','--s2'],en=['2024 season','2025–26 seasons','Current block'];
 const f=figure(P,'Pace against heart rate',
   'For the same effort, are you moving faster? Upper-left is better. Note the eras barely overlap — this summer you have simply not run at the heart rates you used to, so a clean like-for-like comparison does not exist yet. Repeating one fixed-effort run a month would create one.',
   en.map((n,i)=>keyDot(css(ec[i]),n)).join(''));
 scatter(f,D.runs.map(r=>({x:r.hr,y:r.pace,color:ec[era(r.d)],r:5.5,
   tip:`<div class="tt">${dfull(dnum(r.d))} · ${r.km} km</div>`+
       `<div class="tr">Pace <b>${paceFmt(r.pace)} /km</b></div><div class="tr">Avg HR <b>${r.hr} bpm</b></div>`})),
   {h:250,flipY:true,yfmt:v=>paceFmt(v),xfmt:v=>v+' bpm',xlab:'',pl:48,y0:4.5,y1:8.5});}

/* efficiency index */
{const eiRuns=D.runs.filter(r=>r.hrr>=0.55);
 const pts=eiRuns.map(r=>[r.d,r.ei]);
 const f=figure(P,'Aerobic efficiency index',
   'Metres covered per minute, per heartbeat above resting. Rises when the same effort buys more speed — the number that strips out how hard you tried. Very-low-effort runs are excluded: the ratio is unstable when heart rate sits close to resting.',
   keyDot(S1,'One run')+keyLine(S2,'Trend'));
 const sm=[];for(let i=0;i<pts.length;i++){const w=pts.slice(Math.max(0,i-4),i+1);
   sm.push([pts[i][0],Math.round(w.reduce((a,b)=>a+b[1],0)/w.length*10)/10]);}
 timeChart(f,[
   {name:'Run',color:'--s1',type:'dot',pts,r:4,op:.55},
   {name:'5-run avg',color:'--s2',pts:sm,w:2.5,fmt:v=>nfmt(v)}],
   {h:210,snap:9});}

/* table */
{const d=document.createElement('details');
 d.innerHTML=`<summary>All ${D.runs.length} runs — table view</summary>
 <div class="scroll"><table><thead><tr><th>Date</th><th>km</th><th>Time</th><th>Pace</th>
 <th>Avg HR</th><th>Max HR</th><th>Zone</th><th>Eff.</th></tr></thead><tbody>`+
 D.runs.slice().reverse().map(r=>`<tr><td>${r.d}</td><td>${r.km}</td><td>${Math.round(r.min)}′</td>
 <td>${paceFmt(r.pace)}</td><td>${r.hr}</td><td>${r.maxhr??'—'}</td><td>Z${r.zone}</td><td>${r.ei}</td></tr>`).join('')
 +`</tbody></table></div>`;
 P.appendChild(d);}
}
</script>
<script>
/* =================== PANEL 2 — THE ENGINE =================== */
function buildHealth(){
const P=document.getElementById('health');
const S1=css('--s1'),S2=css('--s2'),S3=css('--s3');
const dRHR=(K.rhr_now-K.rhr_then).toFixed(1);

P.appendChild(h(`<div class="hero">
  <div class="n">−${(K.rhr_first-K.rhr_now).toFixed(0)}<small> bpm</small></div>
  <div class="t">resting heart rate since tracking began — from <b>${K.rhr_first}</b> down to
  <b>${K.rhr_now}</b>. This improved steadily <b>even through the months you did not run</b>,
  because your daily movement roughly doubled. It is the strongest signal in your entire dataset.</div></div>`));

P.appendChild(h(lede('engine.intro',()=>`<div class="lede"><h2>The metrics that don't care whether you ran this week</h2>
<p>These are measured 24/7, so unlike pace they are not hostage to your training gaps —
which makes them the fairest measure of whether you are actually getting fitter.
<b>${(()=>{const n=[K.rhr_first-K.rhr_now,K.hrv_now-K.hrv_then,K.whr_first-K.whr_now]
  .filter(x=>x>0).length;return n===3?'All three directly-measured markers are moving the right way.'
  :n===0?'None of the three is moving the right way at the moment.'
  :`${n} of the three are moving the right way; the numbers below say which.`;})()}</b></p>
<p>Resting HR <b>${K.rhr_first} → ${K.rhr_now}</b>, HRV <b>${K.hrv_now} ms</b> (was ${K.hrv_then} a year ago),
walking heart rate <b>${K.whr_first} → ${K.whr_now}</b>, and 2-min recovery <b>${K.hrr_now} bpm</b>.</p>
<p><b>One caveat worth knowing:</b> your VO₂max estimate has fallen from its ${K.vo2_peak[1]} peak
to ${K.vo2_now}. Apple infers it from pace-at-heart-rate on outdoor runs — deliberately running
slow and easy feeds it lower numbers. Given that every directly-measured marker improved over
the same period, read this as an artefact of the training change, not detraining.</p></div>`)));

tiles(P,[
 {l:'Resting heart rate',v:K.rhr_now,u:'bpm',d:`${dRHR} vs a year ago`,c:'up'},
 {l:'HRV (overnight)',v:K.hrv_now,u:'ms',d:`+${(K.hrv_now-K.hrv_then).toFixed(1)} vs a year ago`,c:'up'},
 {l:'2-min HR recovery',v:K.hrr_now,u:'bpm',d:`+${(K.hrr_now-K.hrr_blockstart).toFixed(1)} since ${dstr(D.month.hrr_a_d)}`,c:'up'},
 {l:'Walking heart rate',v:K.whr_now,u:'bpm',d:`was ${K.whr_first} at baseline`,c:'up'},
 {l:'VO₂max estimate',v:K.vo2_now,u:'mL/kg/min',d:`peak was ${K.vo2_peak[1]} · see note`,c:'flat'},
 {l:'Daily steps',v:K.steps_now.toLocaleString(),d:'28-day average'},
]);

const g=document.createElement('div');g.className='grid2';P.appendChild(g);

{const f=figure(g,'Resting heart rate','Daily readings with a 14-day trend. Lower is better.',
  keyDot(S1,'Daily')+keyLine(S2,'14-day trend'));
 timeChart(f,[
  {name:'Daily',color:'--s1',type:'dot',pts:D.rhr_raw,r:2.2,op:.28},
  {name:'Trend',color:'--s2',pts:D.rhr,fmt:v=>nfmt(v)}],{h:230,w:490,ticks:4,domain:[1],pr:40,pl:36});}

{const f=figure(g,'Heart-rate variability','Overnight HRV, 14-day trend. Higher generally means better recovered.',
  keyDot(S3,'Nightly')+keyLine(S1,'14-day trend'));
 timeChart(f,[
  {name:'Nightly',color:'--s3',type:'dot',pts:D.hrv_raw,r:2.2,op:.28},
  {name:'Trend',color:'--s1',pts:D.hrv,fmt:v=>nfmt(v)}],{h:230,w:490,ticks:4,domain:[1],pr:40,pl:36});}

{const f=figure(g,'Heart-rate recovery',
  'How many bpm your heart drops in the 2 minutes after a hard effort. One of the best single proxies for aerobic fitness.',
  keyLine(S3,'Measured recovery')+keyDot(S3,'Individual reading'));
 timeChart(f,[{name:'HR recovery',color:'--s3',pts:D.hrr,gap:70,fmt:v=>nfmt(v)},
  {name:'Reading',color:'--s3',type:'dot',pts:D.hrr,r:3,op:.5,endLabel:false,endDot:false}],
  {h:230,w:490,ticks:4,snap:30,pr:40,pl:36,domain:[0]});}

{const f=figure(g,'Walking heart rate',
  'Average HR while walking — a like-for-like effort you repeat every day, so it is unusually comparable over time.',
  keyLine(S1,'21-day trend'));
 timeChart(f,[{name:'Walking HR',color:'--s1',pts:D.whr,fmt:v=>nfmt(v)}],{h:230,w:490,ticks:4,pr:44,pl:36});}

{const f=figure(P,'VO₂max estimate — read with care',
  'Apple derives this from pace at a given heart rate during outdoor runs, so a deliberate switch to slow easy running pushes it down even as fitness improves.',
  keyLine(S2,'21-day trend'));
 timeChart(f,[{name:'VO₂max',color:'--s2',pts:D.vo2,fill:true,fmt:v=>nfmt(v)}],
  {h:210,ticks:4,bands:[{from:D.month.block_start,to:D.generated,label:'easy-running block',color:'--s3',op:.09}]});}

{const f=figure(P,'Daily activity',
  'Steps and active energy, 28-day averages. The rise through 2025 is what drove resting heart rate down while you were not running.',
  keyLine(S1,'Steps / day')+keyLine(S3,'Active kcal / day'));
 timeChart(f,[{name:'Steps',color:'--s1',pts:D.steps,fmt:v=>Math.round(v).toLocaleString()}],
  {h:200,zero:true,ticks:4,yfmt:v=>(v/1000)+'k'});
 timeChart(f,[{name:'Active energy',color:'--s3',pts:D.kcal,unit:' kcal',fmt:v=>Math.round(v)}],
  {h:170,zero:true,ticks:3});}
}
</script>
<script>
/* =================== PANEL 3 — LOAD & RECOVERY =================== */
function buildRecov(){
const P=document.getElementById('recov');
const S1=css('--s1'),S2=css('--s2'),S3=css('--s3');
const form=K.tsb, formTxt = form>5?'fresh — you have absorbed the work':
  form>-10?'balanced — fitness and fatigue are matched':'loaded — fatigue is running ahead of fitness';

P.appendChild(h(lede('load.intro',()=>`<div class="lede"><h2>Are you building or digging a hole?</h2>
<p><b>Fitness</b> is your 42-day rolling training load, <b>fatigue</b> the 7-day one.
<b>Form</b> is fitness minus fatigue: positive means rested, deeply negative means you are
accumulating more than you are absorbing. Right now form is
<b>${form>0?'+':''}${form}</b> — ${formTxt}.</p>
<p>The pattern to avoid is the one visible in 2024 and 2025: fitness climbs for six to eight
weeks, then the line falls off a cliff for months. <b>Holding fitness above roughly 20 through
autumn would be a bigger win than any single fast run.</b></p>
<p>Sleep duration is quietly the best-behaved part of your data: <b>${K.sleep_now} h</b> a night,
with REM up from ~1.6 h in 2024 to ~2.1 h now. The one thing moving the wrong way is
<b>bedtime</b>, which has drifted from about 22:00 in mid-2024 to past 01:00. It has not cost you
hours yet, but it is the trend worth watching.</p></div>`)));

tiles(P,[
 {l:'Fitness (42-day load)',v:K.ctl},
 {l:'Fatigue (7-day load)',v:K.atl},
 {l:'Form (fitness − fatigue)',v:(form>0?'+':'')+form,d:formTxt,c:form>-10?'up':'dn'},
 {l:'Sleep',v:K.sleep_now,u:'h / night',d:'30-day average'},
]);

{const f=figure(P,'Fitness, fatigue and form',
  'Modelled from every workout\'s duration and heart rate. Fitness is what you keep; fatigue is what you are carrying.',
  keyLine(S1,'Fitness (42-day)')+keyLine(S2,'Fatigue (7-day)')+keyLine(S3,'Form'));
 timeChart(f,[
  {name:'Fitness',color:'--s1',pts:D.load.ctl,fmt:v=>nfmt(v)},
  {name:'Fatigue',color:'--s2',pts:D.load.atl,fmt:v=>nfmt(v),endLabel:false},
  {name:'Form',color:'--s3',pts:D.load.tsb,fmt:v=>nfmt(v),endLabel:false}],
  {h:250,ticks:5,zeroLine:0,pr:52,x0:'2025-06-01'});}

const g=document.createElement('div');g.className='grid2';P.appendChild(g);

{const f=figure(g,'Sleep duration','Nightly hours asleep with a 14-day trend.',
  keyDot(S1,'Nightly')+keyLine(S2,'14-day trend'));
 timeChart(f,[
  {name:'Nightly',color:'--s1',type:'dot',pts:D.sleep_raw,r:2.2,op:.28},
  {name:'Trend',color:'--s2',pts:D.sleep,fmt:v=>nfmt(v),unit:' h'}],{h:230,w:490,ticks:4,domain:[1],pr:48,pl:36});}

{const f=figure(g,'Sleep architecture','Deep and REM sleep per night, 21-day trend. Both have improved since late 2025.',
  keyLine(S1,'REM')+keyLine(S3,'Deep'));
 timeChart(f,[
  {name:'REM',color:'--s1',pts:D.rem,fmt:v=>nfmt(v,2),unit:' h'},
  {name:'Deep',color:'--s3',pts:D.deep,fmt:v=>nfmt(v,2),unit:' h'}],{h:230,w:490,ticks:4,zero:true,pr:52,pl:36});}

{const f=figure(g,'Bedtime — drifting later',
  'When sleep starts, 21-day trend. This has slid from around 22:00 in mid-2024 to past 01:00 now. Duration has held up, so it has not cost you sleep — but it is the one clearly negative trend in your data.',
  keyLine(S3,'Sleep onset'));
 timeChart(f,[{name:'Sleep onset',color:'--s3',pts:D.bed,
   fmt:v=>{const n=(v+24)%24;return `${String(Math.floor(n)).padStart(2,'0')}:${String(Math.round((n%1)*60)).padStart(2,'0')}`;}}],
   {h:230,w:490,ticks:4,pr:52,pl:44,yfmt:v=>{const n=((v%24)+24)%24;return `${String(Math.floor(n)).padStart(2,'0')}:${String(Math.round((n%1)*60)).padStart(2,'0')}`;}});}

{const f=figure(g,'Respiratory rate','Overnight breaths per minute. A sustained rise often precedes illness or overreaching — yours ticked up about 1.5 breaths/min across the July block and has since eased back.',
  keyLine(S2,'21-day trend'));
 timeChart(f,[{name:'Resp. rate',color:'--s2',pts:D.rr,fmt:v=>nfmt(v)}],{h:230,w:490,ticks:4,pr:40,pl:36});}

{const wk=D.weekly.filter(w=>w.load>0);
 const f=figure(P,'Weekly training load','Total modelled stress per week. Useful for spotting jumps — week-to-week rises above ~30% are where injuries cluster.',
  keyLine(S2,'Load per week'));
 timeChart(f,[{name:'Load',color:'--s2',type:'col',pts:D.weekly.map(w=>[w.w,w.load])}],
  {h:190,zero:true,ticks:3,pr:20,snap:5});}
}
</script>
<script>
/* =================== PANEL 4 — METHOD =================== */
function buildAbout(){
const P=document.getElementById('about');
P.appendChild(h(`<div class="lede"><h2>How these numbers were produced</h2>
<p>Built directly from your Apple Health export. Three cleaning steps mattered enough to change
the answers, so they are worth knowing about.</p></div>`));

P.appendChild(h(`<details open><summary>Data cleaning — what was corrected</summary>
<ul class="notes">
<li><b>${K.dupes} duplicate workout records removed.</b>
Your export contains each workout 2–3 times — repeated exports plus a watch rename in July 2026
created copies under "Apple Watch — Maksim", "Apple Watch" and "Maksim's Apple Watch".
Removing them dropped ${D.dupes} duplicate workout records, leaving ${D.n_workouts} distinct
workouts, of which <b>${D.runs.length}</b> are runs carrying heart-rate data and are used
throughout. Weekly mileage was overstated by roughly 2×.</li>
<li><b>Sleep segments merged by interval union.</b> Phone and watch both log sleep, and the segments
overlap. Naively summing them gives ~15 hours a night. Merging overlapping intervals gives the
real figure, ~7.5 h.</li>
<li><b>Steps and energy de-duplicated across devices.</b> iPhone and Watch both count steps; summing
all sources produced ~30,000 steps/day. Each day now takes the single highest-recording source,
giving a realistic ~14,000.</li>
</ul></details>`));

P.appendChild(h(`<details><summary>Definitions</summary>
<ul class="notes">
<li><b>Heart-rate zones</b> are the absolute bands you train by, not a percentage: recovery
&le;149, easy 150–165, grey 166–177, threshold 178–186, VO₂max 187+. A percentage-of-reserve model
was used until 2 Sep 2026; it graded against a resting HR that falls as you get fitter, so its
boundaries drifted about 5 bpm across this dataset and it had no band for the grey zone at all.
Functional max is ${K.hrmax} bpm — the 95th percentile of your per-run maxima, since the single
highest reading is usually an artefact.</li>
<li><b>Aerobic efficiency index</b> = metres per minute ÷ heartbeats above resting, ×10.
Rising means the same cardiac effort is buying more speed.</li>
<li><b>Training load</b> is Banister TRIMP: duration × heart-rate reserve × an exponential
intensity weighting. <b>Fitness</b> and <b>fatigue</b> are 42- and 7-day exponential averages of it;
<b>form</b> is the difference.</li>
<li><b>Trend lines</b> are trailing rolling means over the stated window — 14 days for noisy daily
signals, 21–28 for slower ones.</li>
</ul></details>`));

P.appendChild(h(`<details><summary>Metrics deliberately left out</summary>
<ul class="notes">
<li><b>Six-minute walk distance</b> — pinned at exactly 500 m for all 224 readings. That is Apple's
reporting ceiling, not a measurement. No signal.</li>
<li><b>Walking steadiness</b> — 100% in almost every reading. It is a fall-risk screen for older
adults and will never move for you.</li>
<li><b>Weight</b> — only 8 manual entries across four years (67.5 kg in 2022, 78 kg in 2023,
75 kg in July 2026). Too sparse to trend; worth logging regularly if you want it to mean anything.</li>
<li><b>Blood oxygen</b> — sits at 97–99% with no meaningful variation.</li>
<li><b>Raw pace on its own</b> — shown only alongside heart rate. Pace without effort context
would have made this summer's block look like a regression when it is the opposite.</li>
</ul></details>`));

P.appendChild(h(`<details><summary>What to watch next</summary>
<ul class="notes">
<li><b>Weeks-with-a-run, not weekly mileage.</b> Your ceiling has never been fitness; it has been
the 3–7 month gaps. Getting through October without a two-week break would be a genuine first.</li>
<li><b>Heart-rate recovery and resting HR</b> are your two fastest-responding fitness signals and
are unaffected by the pace you choose to run. Watch these rather than VO₂max.</li>
<li><b>Efficiency at a fixed heart rate.</b> Once a month, run 5 km holding ~165 bpm and record the
pace. Over a base block that pace should drop steadily — that is the cleanest possible progress test.</li>
<li><b>Weekly load jumps.</b> The 13 July week was 263 load units against a prior of near zero.
Keep week-to-week rises under about 30% to stay ahead of injury.</li>
</ul></details>`));
}
</script>
<script>
/* tabs + theme */
const tabs=[...document.querySelectorAll('nav button')];
tabs.forEach(b=>b.onclick=()=>{
  tabs.forEach(x=>x.setAttribute('aria-selected',x===b));
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('on',p.id===b.dataset.p));
  scrollTo({top:0,behavior:'smooth'});});

function renderAll(){
  document.querySelectorAll('.panel').forEach(p=>p.innerHTML='');
  buildMonth(); buildPerf(); buildHealth(); buildRecov(); buildAbout();
}
const tb=document.getElementById('tbtn');
function setT(t){document.documentElement.dataset.theme=t;tb.textContent=t==='dark'?'Light':'Dark';}
/* honour a theme the host already stamped on the root element; fall back to the OS */
const pre=document.documentElement.getAttribute('data-theme');
setT(pre==='dark'||pre==='light'?pre:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'));
renderAll();
tb.onclick=()=>{setT(document.documentElement.dataset.theme==='dark'?'light':'dark');renderAll();};
</script>
</body></html>"""
