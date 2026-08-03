const API='http://127.0.0.1:8766/api';
let capture=null,companies=[];
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function captureApplicationPage(){
  const clean=s=>String(s||'').replace(/\s+/g,' ').trim();
  const sensitive=/(password|密码|验证码|verification|captcha|身份证|个人证件|证件号|id\s*card|银行卡|bank\s*card|cvv|social\s*security|ssn)/i;
  const visible=el=>!el.disabled&&!el.closest('[hidden],[aria-hidden="true"]')&&!!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
  const labelOf=el=>{let text='';if(el.id){try{text=document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText||''}catch(_){}}if(!text)text=el.closest('label')?.innerText||'';if(!text&&el.getAttribute('aria-labelledby'))text=el.getAttribute('aria-labelledby').split(/\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' ');return clean(text||el.getAttribute('aria-label')||el.placeholder||el.name||el.id||'未命名字段');};
  const headingOf=el=>{const fieldset=el.closest('fieldset');if(fieldset){const legend=fieldset.querySelector(':scope>legend');if(legend?.innerText)return clean(legend.innerText)}let node=el.parentElement;for(let depth=0;node&&depth<6;depth++,node=node.parentElement){let prev=node.previousElementSibling;for(let i=0;prev&&i<4;i++,prev=prev.previousElementSibling){if(/^H[1-6]$/.test(prev.tagName)||prev.matches('[role="heading"],.section-title,.form-title')){const t=clean(prev.innerText);if(t)return t}}}return '未分组';};
  const semantic=label=>{const x=label.toLowerCase();const rules=[['name',/(姓名|name)/],['email',/(邮箱|邮件|email)/],['phone',/(电话|手机|phone|mobile)/],['education',/(学校|院校|学历|学位|专业|education|school|university|major|degree)/],['experience',/(实习|工作经历|职位|岗位|experience|employment)/],['project_experience',/(项目|project)/],['availability_date',/(到岗|入职|available|start date)/],['company_motivation',/(为什么.*公司|申请.*原因|motivation|why.*company)/],['self_introduction',/(自我介绍|自我评价|个人优势|introduce yourself|about you)/],['salary',/(薪资|salary|compensation)/],['location',/(地点|城市|location|city)/],['attachment',/(简历|附件|resume|cv|attachment)/]];return rules.find(r=>r[1].test(x))?.[0]||'other';};
  const fields=[],radioDone=new Set();

  document.querySelectorAll('input,textarea,select,[contenteditable="true"]').forEach(el=>{
    if(!visible(el))return;const type=(el.type||el.tagName).toLowerCase();
    if(['password','hidden','submit','button','reset','image','file'].includes(type))return;
    const label=labelOf(el);if(sensitive.test(label)||sensitive.test(el.name||''))return;
    let value='';
    if(type==='radio'){
      const key=el.name||label;if(radioDone.has(key))return;radioDone.add(key);
      const radios=el.name?[...document.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"]`)]:[el];
      const chosen=radios.find(x=>x.checked);if(!chosen)return;value=labelOf(chosen)||chosen.value;
    }else if(type==='checkbox'){if(!el.checked)return;value=el.value&&el.value!=='on'?el.value:'已选择';}
    else if(el.tagName==='SELECT')value=[...el.selectedOptions].map(o=>clean(o.textContent)).filter(Boolean).join('、');
    else value=clean(el.value||el.innerText);
    if(!value||sensitive.test(value)&&sensitive.test(label))return;
    fields.push({section:headingOf(el),label,value,field_type:type,semantic_key:semantic(label),required:!!el.required});
  });

  // “简历查看页”没有 input：从可见章节和标签—内容对恢复结构。
  const sectionPattern=/(基本信息|个人信息|求职意向|教育经历|教育背景|工作经历|实习经历|项目经历|校园经历|社团经历|获奖|荣誉|证书|语言能力|技能|自我评价|自我介绍|家庭信息|附件|作品)/i;
  const labelPattern=/(姓名|性别|出生|邮箱|邮件|手机|电话|工作地点|期望地点|政治面貌|学校|院校|学历|学位|专业|学院|入学|毕业|起止时间|开始时间|结束时间|公司|单位|部门|职位|岗位|项目名称|项目角色|职责|描述|成绩|排名|语言|证书|奖项|到岗|薪资|链接|作品|个人优势|自我评价)/i;
  const ignored=/(首页|职位|招聘|登录|退出|编辑|删除|新增|保存|取消|返回|技术人才项目|所有书签|完成更新)/i;
  const leafText=el=>{
    if(!visible(el)||el.closest('nav,header,footer,[role="navigation"],script,style,noscript'))return '';
    const own=[...el.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join(' ');
    return clean(own||(!el.children.length?el.innerText:''));
  };
  const nodes=[...document.querySelectorAll('main *,[role="main"] *,body *')];
  const visited=new Set(),tokens=[];let currentSection='页面信息';
  for(const el of nodes){
    if(visited.has(el))continue;visited.add(el);
    const text=leafText(el);if(!text||text.length>1600)continue;
    const isHeading=/^H[1-6]$/.test(el.tagName)||el.getAttribute('role')==='heading'||(sectionPattern.test(text)&&text.length<30);
    if(isHeading&&sectionPattern.test(text)){currentSection=text;continue;}
    if(ignored.test(text)&&text.length<20)continue;
    const className=typeof el.className==='string'?el.className:'';
    tokens.push({text,section:currentSection,isLabel:(labelPattern.test(text)&&text.length<42)||/(label|field-name|item-name)/i.test(className)});
  }

  const existing=new Set(fields.map(f=>`${f.section}|${f.label}|${f.value}`));
  for(let i=0;i<tokens.length;i++){
    const item=tokens[i];if(!item.isLabel||sensitive.test(item.text))continue;
    let valueItem=null,valueParts=[];
    for(let j=i+1;j<Math.min(tokens.length,i+5);j++){
      if(tokens[j].section!==item.section||tokens[j].isLabel)break;
      if(tokens[j].text!==item.text){valueItem=valueItem||tokens[j];valueParts.push(tokens[j].text);if(semantic(item.text)!=='phone')break;}
    }
    if(!valueItem||sensitive.test(valueItem.text))continue;
    let capturedValue=semantic(item.text)==='phone'?valueParts.join(' ').replace(/\s+/g,' ').trim():valueItem.text;
    if(semantic(item.text)==='phone'){
      const phoneMatch=capturedValue.match(/(?:\+?\d{1,3}[\s-]*)?(?:\d[\s-]*){7,15}/);
      if(phoneMatch)capturedValue=phoneMatch[0].trim();
    }
    const key=`${item.section}|${item.text}|${capturedValue}`;if(existing.has(key))continue;existing.add(key);
    fields.push({section:item.section,label:item.text,value:capturedValue,field_type:'display_text',semantic_key:semantic(item.text),required:false});
  }

  // 明确章节无法拆成字段时，保留完整章节正文，避免项目/经历长文本丢失。
  const sectionNames=[...new Set(tokens.map(t=>t.section).filter(x=>x!=='页面信息'))];
  for(const section of sectionNames){
    if(fields.some(f=>f.section===section))continue;
    const content=[...new Set(tokens.filter(t=>t.section===section&&!sensitive.test(t.text)).map(t=>t.text))].join('\n').slice(0,6000);
    if(content.length>3)fields.push({section,label:'章节内容',value:content,field_type:'display_text',semantic_key:semantic(section),required:false});
  }

  const groups=new Map();
  fields.forEach(f=>{if(!groups.has(f.section))groups.set(f.section,[]);const {section,...rest}=f;groups.get(section).push(rest);});
  const hasEditable=[...document.querySelectorAll('input,textarea,select,[contenteditable="true"]')].some(visible);
  return {title:document.title,url:location.href,host:location.host,structure:[...groups].map(([title,items])=>({title,fields:items})),capture_mode:hasEditable?'form_and_page':'read_only_page'};
}

function guessCompany(title,host){
  const aliases={
    'bytedance.com':'字节跳动','zijieapi.com':'字节跳动','baidu.com':'百度',
    'alibaba.com':'阿里巴巴','aliyun.com':'阿里巴巴','tencent.com':'腾讯',
    'meituan.com':'美团','iflytek.com':'科大讯飞','cmbchina.com':'招商银行'
  };
  const alias=Object.entries(aliases).find(([domain])=>host.endsWith(domain))?.[1];
  if(alias&&companies.some(c=>c.name===alias))return alias;
  const known=companies.find(c=>title.includes(c.name)||host.includes(c.name.toLowerCase()));
  return known?.name||'';
}
function syncTracks(){const company=companies.find(x=>x.name===$('#company').value),select=$('#track');select.innerHTML='<option value="">不指定岗位</option>'+((company?.tracks||[]).map(t=>`<option value="${t.id}">${esc(t.role)}</option>`).join(''));}
function paint(){
  const fields=capture.structure.flatMap(x=>x.fields);
  $('#count').textContent=`${fields.length} 个字段`;$('#groups').textContent=`${capture.structure.length} 个章节`;
  $('#preview').innerHTML=`<div class="capture-mode">${capture.capture_mode==='read_only_page'?'已识别只读简历页':'已识别表单与页面内容'}</div>`+capture.structure.map(g=>`<section class="group"><h2><span>${esc(g.title)}</span><small>${g.fields.length}</small></h2>${g.fields.slice(0,8).map(f=>`<div class="field"><b>${esc(f.label)}</b><span>${esc(f.value)}</span><small>${esc(f.semantic_key)}</small></div>`).join('')}</section>`).join('');
  $('#status').hidden=true;$('#form').hidden=false;
}
async function init(){
  try{
    const data=await fetch(API+'/browser-capture/companies').then(r=>{if(!r.ok)throw new Error('请先启动 Caddie');return r.json();});companies=data.companies||[];
    const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
    const result=await chrome.scripting.executeScript({target:{tabId:tab.id},world:'MAIN',func:captureApplicationPage});capture=result[0]?.result;
    if(!capture||!capture.structure.length)throw new Error('没有识别到可保存的简历内容；请切换到简历详情页或编辑页后重试');
    $('#company').innerHTML='<option value="">选择公司</option>'+companies.map(c=>`<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('');
    $('#company').value=guessCompany(capture.title,capture.host);syncTracks();paint();
  }catch(e){$('#status').textContent=e.message;$('#status').classList.add('error');}
}
$('#company').addEventListener('change',syncTracks);
$('#save').addEventListener('click',async()=>{const company=$('#company').value;if(!company)return alert('请选择公司');const button=$('#save');button.disabled=true;button.textContent='保存中…';try{const r=await fetch(API+'/browser-capture/forms',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({company,track_id:Number($('#track').value)||null,title:capture.title,page_url:capture.url,portal_host:capture.host,structure:capture.structure})});const data=await r.json();if(!r.ok)throw new Error(data.detail||'保存失败');button.textContent=`已保存 ${data.field_count} 个字段`;setTimeout(()=>window.close(),900);}catch(e){button.disabled=false;button.textContent='重新保存';alert(e.message);}});
init();
