fn main() {
    // The bundled Python (resource `python`) is downloaded by `npm run python:fetch`, which `tauri dev` and
    // `tauri build` run first. Plain `cargo build` / `cargo test` (CI, editors) only need the folder to exist; the
    // app then falls back to a system Python.
    let python = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("python");
    if !python.exists() {
        let _ = std::fs::create_dir_all(&python);
    }
    tauri_build::build()
}
