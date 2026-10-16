# Contributing

Thanks for your interest. The project is at an early stage (0.1.0); issues and focused pull requests are welcome.

- Keep changes small and focused; one topic per pull request.
- Run `npm run typecheck`, `npm test`, `npm run test:rust` and `npm run test:python` before opening a pull request.
- The dashboard renderer (`src/view/view.ts`) and stylesheet (`src/styles/research-dashboard.css`) are ports of a
  reference design; keep DOM structure and class names stable so the stylesheet keeps applying.
- The collector (`collector/`) is vendored; see `collector/VENDORED.md` for its provenance. Changes that also apply
  upstream should say so in the pull request.
- Never make the app or collector write to, compile or run anything inside a monitored source folder.

By contributing you agree that your contributions are licensed under the MIT License.
