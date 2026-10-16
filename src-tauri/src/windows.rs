//! Main window and floating widget helpers.

use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};

use crate::settings::{SettingsState, WidgetBounds, WIDGET_MIN, WIDGET_SIZE};

pub const MAIN: &str = "main";
pub const WIDGET: &str = "widget";

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
#[serde(rename_all = "camelCase")]
pub struct NavigateTarget {
    pub project_id: Option<String>,
    pub source_id: Option<String>,
    pub chapter_id: Option<String>,
}

pub fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// Bring the main window forward and ask it to open a page (used by the widget and the tray).
pub fn navigate(app: &AppHandle, target: NavigateTarget) {
    show_main(app);
    let _ = app.emit_to(MAIN, "rd://navigate", target);
}

pub fn widget_visible(app: &AppHandle) -> bool {
    app.get_webview_window(WIDGET).and_then(|w| w.is_visible().ok()).unwrap_or(false)
}

fn ensure_widget(app: &AppHandle) -> tauri::Result<tauri::WebviewWindow> {
    if let Some(window) = app.get_webview_window(WIDGET) {
        return Ok(window);
    }
    let settings = app.state::<SettingsState>().get();
    let bounds = settings.widget.bounds.filter(|b| on_a_monitor(app, b));
    let (width, height) = bounds.map(|b| (b.width, b.height)).unwrap_or(WIDGET_SIZE);
    let mut builder = WebviewWindowBuilder::new(app, WIDGET, WebviewUrl::App("index.html".into()))
        .title("Research")
        .inner_size(width, height)
        .min_inner_size(WIDGET_MIN.0, WIDGET_MIN.1)
        .decorations(false)
        .resizable(true)
        .skip_taskbar(true)
        .always_on_top(settings.widget.keep_on_top)
        .visible(false)
        .transparent(true)
        .shadow(false);
    if let Some(b) = bounds {
        builder = builder.position(b.x, b.y);
    }
    builder.build()
}

/// A saved position is used only while some monitor still shows the widget's top bar (monitors change).
fn on_a_monitor(app: &AppHandle, bounds: &WidgetBounds) -> bool {
    let Ok(monitors) = app.available_monitors() else { return false };
    monitors.iter().any(|monitor| {
        let scale = monitor.scale_factor();
        let (x, y) = (monitor.position().x as f64 / scale, monitor.position().y as f64 / scale);
        let (w, h) = (monitor.size().width as f64 / scale, monitor.size().height as f64 / scale);
        bounds.x + 40.0 < x + w && bounds.x + bounds.width - 40.0 > x && bounds.y >= y - 4.0 && bounds.y + 20.0 < y + h
    })
}

static BOUNDS_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// The widget moved or was resized: save its bounds half a second after the last change.
pub fn remember_widget_bounds(window: &tauri::Window) {
    let app = window.app_handle().clone();
    let (Ok(position), Ok(size), Ok(scale)) = (window.outer_position(), window.inner_size(), window.scale_factor()) else { return };
    if window.is_minimized().unwrap_or(false) || !window.is_visible().unwrap_or(false) {
        return;
    }
    let bounds = WidgetBounds { x: position.x as f64 / scale, y: position.y as f64 / scale, width: size.width as f64 / scale, height: size.height as f64 / scale };
    let Some(bounds) = bounds.sane() else { return };
    let seq = BOUNDS_SEQ.fetch_add(1, Ordering::SeqCst) + 1;
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(500));
        if BOUNDS_SEQ.load(Ordering::SeqCst) != seq {
            return;
        }
        let state = app.state::<SettingsState>();
        let mut settings = state.get();
        if settings.widget.bounds == Some(bounds) {
            return;
        }
        settings.widget.bounds = Some(bounds);
        if let Ok(saved) = state.save(settings) {
            let _ = app.emit("settings://changed", saved);
        }
    });
}

pub fn set_widget_visible(app: &AppHandle, visible: bool) -> Result<bool, String> {
    let state = app.state::<SettingsState>();
    if visible {
        let window = ensure_widget(app).map_err(|e| format!("The widget could not open: {e}"))?;
        window.show().map_err(|e| e.to_string())?;
        track_widget_pointer(app);
        let _ = window.set_focus();
    } else if let Some(window) = app.get_webview_window(WIDGET) {
        window.hide().map_err(|e| e.to_string())?;
    }
    let mut settings = state.get();
    if settings.widget.visible != visible {
        settings.widget.visible = visible;
        if let Ok(saved) = state.save(settings) {
            let _ = app.emit("settings://changed", saved);
        }
    }
    crate::tray::sync(app);
    Ok(visible)
}

/// Whether the pointer is over the widget now (None when the widget is hidden or the position is unavailable).
pub fn widget_pointer_inside(app: &AppHandle) -> Option<bool> {
    let window = app.get_webview_window(WIDGET).filter(|w| w.is_visible().unwrap_or(false))?;
    let point = app.cursor_position().ok()?;
    let origin = window.outer_position().ok()?;
    let size = window.outer_size().ok()?;
    Some(contains((point.x, point.y), (origin.x, origin.y), (size.width, size.height)))
}

static TRACKING: AtomicBool = AtomicBool::new(false);

/// Whether a screen point lies inside a window rectangle (physical pixels).
pub fn contains(point: (f64, f64), origin: (i32, i32), size: (u32, u32)) -> bool {
    let (x, y) = point;
    x >= origin.0 as f64 && y >= origin.1 as f64 && x < origin.0 as f64 + size.0 as f64 && y < origin.1 as f64 + size.1 as f64
}

/// Watch the pointer against the widget's frame and tell the widget when it enters or leaves (`widget://pointer`).
/// The page's own pointer events miss leaves while another app is active or when the pointer moves off quickly; the
/// idle fade follows this signal instead (and asks `widget_pointer_inside` once on load, since a change can fire before
/// the page listens). One thread for the app's lifetime; it idles while the widget is hidden.
pub fn track_widget_pointer(app: &AppHandle) {
    if TRACKING.swap(true, Ordering::SeqCst) {
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        let mut last: Option<bool> = None;
        loop {
            if !widget_visible(&app) {
                last = None;
                std::thread::sleep(Duration::from_millis(400));
                continue;
            }
            let inside = widget_pointer_inside(&app);
            if inside.is_some() && inside != last {
                last = inside;
                let _ = app.emit_to(WIDGET, "widget://pointer", inside.unwrap_or(false));
            }
            std::thread::sleep(Duration::from_millis(120));
        }
    });
}

pub fn apply_widget_settings(app: &AppHandle) {
    let settings = app.state::<SettingsState>().get();
    if let Some(window) = app.get_webview_window(WIDGET) {
        let _ = window.set_always_on_top(settings.widget.keep_on_top);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pointer_inside_the_frame_only() {
        assert!(contains((100.0, 100.0), (50, 60), (360, 560)));
        assert!(contains((50.0, 60.0), (50, 60), (360, 560)));
        assert!(!contains((410.0, 100.0), (50, 60), (360, 560)));
        assert!(!contains((100.0, 59.5), (50, 60), (360, 560)));
        assert!(contains((-10.0, 5.0), (-100, 0), (200, 100)));
    }
}
