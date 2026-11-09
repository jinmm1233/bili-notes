/* BiliNotes 离线学习包的 Service Worker。
 *
 * 只接管离线包自己的请求，别碰主应用：否则主应用的 /api/* 会被缓存成旧数据，
 * 电脑上点啥都像没生效。
 *
 * 缓存范围：
 *   /offline            页面本体（网络优先，离线回缓存）
 *   /api/offline/bundle 数据（网络优先，离线回缓存）
 *   /static/*           KaTeX / 图标 / manifest（缓存优先）
 *   /api/library/*/file/* 笔记里的截图（缓存优先，读哪篇缓存哪篇）
 */

const VERSION = 'bn-offline-v1';
const SHELL = [
  '/offline',
  '/static/vendor/katex/katex.min.css',
  '/static/vendor/katex/katex.min.js',
  '/static/manifest.webmanifest',
  '/static/icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      .then((cache) => Promise.all(SHELL.map((url) => cache.add(url).catch(() => {}))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

function put(req, res) {
  if (!res || !res.ok) return res;
  const copy = res.clone();
  caches.open(VERSION).then((cache) => cache.put(req, copy)).catch(() => {});
  return res;
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  const path = url.pathname;

  // 数据与页面：网络优先（每次连上电脑都是新的），断网用缓存
  if (path === '/api/offline/bundle' || path === '/offline') {
    event.respondWith(
      fetch(req).then((res) => put(req, res)).catch(() => caches.match(req).then((hit) => hit || caches.match('/offline'))),
    );
    return;
  }

  // 静态资源与笔记图片：缓存优先，命中就秒开
  if (path.startsWith('/static/') || /^\/api\/library\/.+\/file\//.test(path)) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => put(req, res))),
    );
    return;
  }

  // 其他请求（主应用、其他接口）一概不拦
});
