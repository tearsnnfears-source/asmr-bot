/** @type {import('next').NextConfig} */
const nextConfig = {
  // next/image: разрешаем загрузку картинок с наших поддоменов
  images: {
    remotePatterns: [
      { protocol: 'https', hostname: 'img.asmrleaks.tv',   pathname: '/**' },
      { protocol: 'https', hostname: 'files.asmrleaks.tv', pathname: '/**' },
      { protocol: 'https', hostname: 'cdn.asmrleaks.tv',   pathname: '/**' },
      // Telegram user avatars (на случай если ты где-то рендеришь TG аватарки)
      { protocol: 'https', hostname: 't.me',               pathname: '/**' },
    ],
  },

  // Глобальные заголовки:
  //  - CSP: разрешаем картинки/медиа/HLS с наших поддоменов
  //  - Permissions-Policy и X-Content-Type-Options для безопасности
  async headers() {
    const csp = [
      "default-src 'self'",
      // inline разрешен для next/script и подобного
      "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://telegram.org https://t.me",
      "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
      "font-src 'self' data: https://fonts.gstatic.com",
      // IMG: поддомены asmrleaks.tv + avatars Telegram
      "img-src 'self' data: blob: https://img.asmrleaks.tv https://files.asmrleaks.tv https://cdn.asmrleaks.tv https://iframe.mediadelivery.net https://t.me",
      // MEDIA (видео/HLS/audio)
      "media-src 'self' blob: https://files.asmrleaks.tv https://cdn.asmrleaks.tv https://img.asmrleaks.tv https://iframe.mediadelivery.net",
      // XHR / fetch (в том числе на твой Railway бэкенд — подправь при необходимости)
      "connect-src 'self' https://*.asmrleaks.tv https://api.telegram.org https://*.railway.app",
      // Встраивание iframe (Bunny Stream, Telegram)
      "frame-src 'self' https://iframe.mediadelivery.net https://t.me https://*.telegram.org",
      "worker-src 'self' blob:",
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
    ].join('; ');

    return [
      {
        source: '/:path*',
        headers: [
          { key: 'Content-Security-Policy', value: csp },
          { key: 'X-Content-Type-Options',   value: 'nosniff' },
          { key: 'Referrer-Policy',          value: 'strict-origin-when-cross-origin' },
          { key: 'Permissions-Policy',       value: 'camera=(), microphone=(), geolocation=()' },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
