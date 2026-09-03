/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  env: { NEXT_PUBLIC_API: process.env.NEXT_PUBLIC_API ?? "http://127.0.0.1:8099" },
};
export default nextConfig;
