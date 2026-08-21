/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // The falkordb client pulls in node:net / node:tls. Keep it out of the
  // bundler's rewrite path so the serverless function requires it at runtime.
  serverExternalPackages: ['falkordb'],
};

export default nextConfig;
