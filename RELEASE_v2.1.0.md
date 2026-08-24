# v2.1.0: Pro UI Overhaul ✨

This release gives Continuity Studio a full visual glow-up plus a batch of stability and security fixes under the hood.

## 🎨 UI & Design
- **Glassmorphism theme**: frosted panels, glass borders, and an animated aurora background
- **Micro-interactions**: cursor glow, button ripples, card 3D tilt, and a top loading bar for API calls
- **Staggered animations**: frames, cards, and storyboard shots now animate in on scroll
- **Fixed fonts**: Bricolage Grotesque, Hanken Grotesk, and JetBrains Mono now actually load (were silently falling back before)
- **Mobile responsive**: tabs scroll horizontally, storyboard shots stack, bigger tap targets, breakpoints at 768px and 480px
- **Accessibility**: keyboard focus rings on all interactive elements, respects `prefers-reduced-motion`
- **Keyboard shortcuts**: Ctrl/Cmd + 1-9 to switch tabs

## 🔒 Security
- Rate limiting on auth endpoints (brute-force protection)
- File upload validation (blocks executable disguises)
- Atomic state writes (no more corruption from concurrent requests)
- Stronger password hashing (310k PBKDF2 iterations)

## ⚡ Performance & Reliability
- Async image-gen queue with circuit breaker and exponential backoff
- Safe subprocess handling: ffmpeg processes are properly killed on timeout (no more zombies)
- Standardized API responses and structured logging

## 🐳 DevOps
- Docker + docker-compose support, serving the pro UI out of the box
- Basic test suite (`pytest tests/`)

## 📝 Notes
The pro UI is served via `ui_patch:app` (already wired into Docker). Running locally? Use `uvicorn ui_patch:app` instead of `uvicorn app:app`.
