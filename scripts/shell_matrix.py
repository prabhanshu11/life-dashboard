# Headless verification matrix for the site shell (docs/site-shell-2026-09-24.md, "Fix 21:xx").
# Run the app on :8391 from pi/, then: uv run --no-project --with playwright python scripts/shell_matrix.py out.json
import json, sys, collections
from playwright.sync_api import sync_playwright
B="http://127.0.0.1:8391"
PAGES=["/","/#time","/#cal","/#fin","/day","/week","/month"]
JS=r"""()=>{
 const rgb=s=>{const m=s.match(/[\d.]+/g).map(Number);return {r:m[0],g:m[1],b:m[2],a:m.length>3?m[3]:1}};
 const lum=c=>{const f=v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)};return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b)};
 const bgOf=e=>{let layers=[];for(let n=e;n;n=n.parentElement){const c=rgb(getComputedStyle(n).backgroundColor);if(c.a>0){layers.push(c);if(c.a>=1)break;}}
   let base={r:255,g:255,b:255};for(const c of layers.reverse()){base={r:c.r*c.a+base.r*(1-c.a),g:c.g*c.a+base.g*(1-c.a),b:c.b*c.a+base.b*(1-c.a)}}return base};
 const vis=e=>e.getClientRects().length>0&&getComputedStyle(e).visibility!=="hidden";
 const out=[];
 document.querySelectorAll(".site-brand,.site-nav a,.site-tools .shell-btn").forEach(e=>{ if(!vis(e))return;
   const fg=rgb(getComputedStyle(e).color),bg=bgOf(e);const L1=lum(fg),L2=lum(bg);
   out.push({t:e.textContent.trim().slice(0,14),cr:+((Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05)).toFixed(2)})});
 const lowop=[];document.querySelectorAll(".site-header, .site-header *, body *").forEach(e=>{ if(!vis(e))return;
   const o=parseFloat(getComputedStyle(e).opacity); if(o<1) lowop.push((e.closest('.site-header')?'HEADER ':'')+e.tagName.toLowerCase()+(e.className&&typeof e.className==='string'?'.'+e.className.split(' ').join('.'):'')+'='+o)});
 const tog=document.getElementById('theme-toggle');
 return {contrast:out,lowop:[...new Set(lowop)],toggle_visible:tog?vis(tog):null,html_theme:document.documentElement.dataset.theme||null,
   body_shell:document.body.dataset.shellTheme||null};
}"""
res=[];
with sync_playwright() as p:
    br=p.chromium.launch()
    for theme in ["unset","light","dark"]:
      for scheme in ["light","dark"]:
        for w in [1896,1440,1024]:
          ctx=br.new_context(viewport={"width":w,"height":1030},color_scheme=scheme)
          if theme!="unset": ctx.add_init_script(f"localStorage.setItem('lifeos-theme','{theme}')")
          for path in PAGES:
            pg=ctx.new_page(); errs=[]
            pg.on("console", lambda m, errs=errs: errs.append(m.text) if m.type=="error" else None)
            pg.on("pageerror", lambda e, errs=errs: errs.append("PAGEERROR "+str(e)))
            pg.goto(B+path); pg.wait_for_timeout(900)
            r=pg.evaluate(JS); r.update(path=path,theme=theme,scheme=scheme,w=w,errs=errs)
            if path in ("/","/day") and w==1896: pg.screenshot(path=f"v_{(path.strip('/#') or 'home')}_{theme}_{scheme}.png")
            res.append(r); pg.close()
          ctx.close()
    br.close()
json.dump(res,open(sys.argv[1] if len(sys.argv)>1 else "matrix.json","w"),indent=1)
