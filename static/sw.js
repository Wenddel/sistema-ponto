// Service Worker - PWA Sistema de Ponto
const CACHE_NAME = 'ponto-clinica-v1';
const URLS_CACHE = [
    '/',
    '/admin',
    '/login',
    '/static/logo.png',
    '/static/manifest.json'
];

// Instalação
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(URLS_CACHE).catch(() => {});
        })
    );
    self.skipWaiting();
});

// Ativação
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.filter((name) => name !== CACHE_NAME)
                    .map((name) => caches.delete(name))
            );
        })
    );
    self.clients.claim();
});

// Fetch - rede primeiro, depois cache
self.addEventListener('fetch', (event) => {
    const req = event.request;
    
    // Apenas GET
    if (req.method !== 'GET') return;
    
    // Para API, sempre usar rede
    if (req.url.includes('/api/')) return;
    
    event.respondWith(
        fetch(req).then((response) => {
            // Atualiza cache com nova resposta
            const respClone = response.clone();
            caches.open(CACHE_NAME).then((cache) => {
                cache.put(req, respClone).catch(() => {});
            });
            return response;
        }).catch(() => {
            // Falha na rede - tenta cache
            return caches.match(req).then((cached) => {
                return cached || caches.match('/admin');
            });
        })
    );
});
