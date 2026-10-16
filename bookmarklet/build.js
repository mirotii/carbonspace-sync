// Builds the plugin and its install page: `node build.js`
//   ../../CarbonSpace Sync - plugin.js     the plugin itself, shared through a Dropbox link
//   ../../CarbonSpace Sync - install.html  page with the bookmark to drag
// If plugin-url.txt holds the plugin's shared Dropbox link, the bookmark is a small
// loader that fetches the latest plugin on every click (no re-install after updates).
// Without it, the whole plugin is put inside the bookmark.
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..', '..');
const src = fs.readFileSync(path.join(__dirname, 'cs-sync.js'), 'utf8');
// Drop comment-only lines and indentation; keep line breaks so ASI stays intact.
const code = src.split(/\r?\n/).map(l => l.trim()).filter(l => l && !l.startsWith('//')).join('\n');
const built = new Date().toISOString().slice(0, 10);
fs.writeFileSync(path.join(root, 'CarbonSpace Sync - plugin.js'), `// CarbonSpace Sync - developed by Unknown Works - built ${built}\n` + code + '\n');

// Turn a Dropbox share link into a direct download link that browsers can fetch.
function rawDropboxUrl(link) {
  const u = new URL(link.trim());
  if (/(^|\.)dropbox\.com$/.test(u.hostname)) {
    u.hostname = 'dl.dropboxusercontent.com';
    u.searchParams.delete('dl');
    u.searchParams.delete('raw');
  }
  return u.toString();
}

const urlFile = path.join(__dirname, 'plugin-url.txt');
let href, mode;
if (fs.existsSync(urlFile) && fs.readFileSync(urlFile, 'utf8').trim()) {
  const url = rawDropboxUrl(fs.readFileSync(urlFile, 'utf8'));
  const loader = `(function(){var u=${JSON.stringify(url)};` +
    `fetch(u+(u.indexOf('?')<0?'?':'&')+'t='+Date.now(),{cache:'no-store'})` +
    `.then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.text();})` +
    `.then(function(t){(0,eval)(t);})` +
    `.catch(function(e){alert('CarbonSpace Sync could not be loaded: '+e.message);});})();`;
  href = 'javascript:' + encodeURIComponent(loader);
  mode = `loader -> ${url}`;
} else {
  href = 'javascript:' + encodeURIComponent(code);
  mode = 'self-contained';
}
const tpl = fs.readFileSync(path.join(__dirname, 'install.template.html'), 'utf8');
const out = path.join(root, 'CarbonSpace Sync - install.html');
fs.writeFileSync(out, tpl.replace('__HREF__', href.replace(/"/g, '%22')).replace('__BUILT__', built));
console.log(`wrote ${out} (bookmark ${(href.length / 1024).toFixed(1)} KB, ${mode})`);
