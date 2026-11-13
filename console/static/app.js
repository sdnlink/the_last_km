import {i18n} from './i18n.js';
import {initialize} from './core.js';
import {initializeLanguage} from './i18n.js';
initializeLanguage();
const legacy=/^#\/(overview|tasks|progress|resources)(?:\/([a-z0-9-]+))?$/.exec(location.hash);
if(legacy){location.replace('/'+legacy[1]+(legacy[2]?'/'+legacy[2]:''));}
else if(document.body.dataset.needsSetup==='true'){location.replace('/setup');}
else {
  initialize();
  const pages={overview:()=>import('./pages/overview.js'),tasks:()=>import('./pages/tasks.js'),progress:()=>import('./pages/progress.js'),resources:()=>import('./pages/resources.js'),detail:()=>import('./pages/detail.js'),setup:()=>import('./pages/setup.js')};
  try{(await pages[document.body.dataset.page]()).mount();}
  catch(error){document.querySelector('#connection-error').hidden=false;document.querySelector('#connection-error').textContent=i18n`页面加载失败：${error.message}`;}
}
