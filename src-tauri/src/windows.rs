//! Main window and floating widget helpers.

use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, WebviewUrl, WebviewWindowBuilder};

use crate::settings::{OrbPoint, SettingsState, WidgetBounds, WIDGET_MIN, WIDGET_SIZE};

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
    let collapsed = settings.widget.collapsed;
    let mut builder = WebviewWindowBuilder::new(app, WIDGET, WebviewUrl::App("index.html".into()))
        .title("Monitoring Anything widget")
        .decorations(false)
        .skip_taskbar(true)
        .always_on_top(settings.widget.keep_on_top)
        .visible(false)
        .transparent(true)
        .shadow(false);
    if collapsed {
        builder = builder.inner_size(ORB, ORB).resizable(false);
        if let Some(anchor) = orb_anchor(app, &settings.widget) {
            builder = builder.position(anchor.x, anchor.y);
        }
        set_geometry(Shape::Orb, (0.0, 0.0));
    } else {
        builder = builder.inner_size(width, height).min_inner_size(WIDGET_MIN.0, WIDGET_MIN.1).resizable(true);
        if let Some(b) = bounds {
            builder = builder.position(b.x, b.y);
        }
        set_geometry(Shape::Panel, (0.0, 0.0));
    }
    builder.build()
}

/* ---------- The circle widget (orb) ----------
 * The widget window takes one of three shapes: the panel (resizable), the orb (a small square holding the circle) and
 * the orb with its flyout (wide enough for the breakdown beside the circle, on the side with room). Changing shape
 * moves the window so the circle stays where it is on screen. The circle's position is saved when it is dragged. */

/// The orb window's side (logical pixels): a 64 px circle plus room for its shadow and hover growth.
pub const ORB: f64 = 72.0;
const FLYOUT_GAP: f64 = 6.0;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Shape {
    Panel,
    Orb,
    Flyout,
}

/// The current shape and where the circle sits inside the window (logical pixels).
static GEOMETRY: Mutex<(Shape, (f64, f64))> = Mutex::new((Shape::Panel, (0.0, 0.0)));

fn set_geometry(shape: Shape, offset: (f64, f64)) {
    if let Ok(mut geometry) = GEOMETRY.lock() {
        *geometry = (shape, offset);
    }
}

fn geometry() -> (Shape, (f64, f64)) {
    GEOMETRY.lock().map(|g| *g).unwrap_or((Shape::Panel, (0.0, 0.0)))
}

/// A screen rectangle in logical pixels.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct OrbLayout {
    pub shape: String,
    /// "right" or "left": where the flyout opens.
    pub side: String,
    /// The circle's window's top-left corner inside the window.
    pub orb_x: f64,
    pub orb_y: f64,
    pub width: f64,
    pub height: f64,
}

/// The window for the orb at `anchor` with a flyout of `flyout` (width, height) beside it, inside the work `area`: the
/// flyout opens to the right unless only the left has room; near the bottom the window rises (the circle never moves).
pub fn flyout_window(anchor: (f64, f64), flyout: (f64, f64), area: Rect) -> (Rect, (f64, f64), bool) {
    let width = ORB + FLYOUT_GAP + flyout.0;
    let height = ORB.max(flyout.1);
    let left = anchor.0 + width > area.x + area.w && anchor.0 + ORB - width >= area.x;
    let x = if left { anchor.0 + ORB - width } else { anchor.0 };
    let y = if anchor.1 + height > area.y + area.h { (area.y + area.h - height).max(area.y).min(anchor.1) } else { anchor.1 };
    (Rect { x, y, w: width, h: height }, (anchor.0 - x, anchor.1 - y), left)
}

/// The panel opened from the orb: its saved size, its top-right corner at the circle's, kept inside the work area.
pub fn panel_window(anchor: (f64, f64), size: (f64, f64), area: Rect) -> Rect {
    let x = (anchor.0 + ORB - size.0).min(area.x + area.w - size.0).max(area.x);
    let y = anchor.1.min(area.y + area.h - size.1).max(area.y);
    Rect { x, y, w: size.0, h: size.1 }
}

/// The work area (logical) of the monitor showing `point`, or of the first monitor.
fn work_area(app: &AppHandle, point: (f64, f64)) -> Option<Rect> {
    let monitors = app.available_monitors().ok()?;
    let areas: Vec<Rect> = monitors.iter().map(|monitor| {
        let scale = monitor.scale_factor();
        let area = monitor.work_area();
        Rect { x: area.position.x as f64 / scale, y: area.position.y as f64 / scale, w: area.size.width as f64 / scale, h: area.size.height as f64 / scale }
    }).collect();
    areas.iter().copied().find(|a| point.0 >= a.x && point.0 < a.x + a.w && point.1 >= a.y && point.1 < a.y + a.h).or_else(|| areas.first().copied())
}

/// Where the circle goes: its saved place while a monitor shows it, else the panel's top-right corner.
fn orb_anchor(app: &AppHandle, widget: &crate::settings::WidgetSettings) -> Option<OrbPoint> {
    if let Some(orb) = widget.orb {
        if work_area(app, (orb.x + ORB / 2.0, orb.y + ORB / 2.0)).is_some_and(|a| orb.x + ORB / 2.0 >= a.x && orb.x + ORB / 2.0 < a.x + a.w && orb.y + ORB / 2.0 >= a.y && orb.y + ORB / 2.0 < a.y + a.h) {
            return Some(orb);
        }
    }
    widget.bounds.map(|b| OrbPoint { x: b.x + b.width - ORB - 6.0, y: b.y + 6.0 })
}

/// The circle's position on screen now (window position plus its offset inside the window).
fn current_anchor(window: &tauri::WebviewWindow) -> Option<(f64, f64)> {
    let scale = window.scale_factor().ok()?;
    let position = window.outer_position().ok()?;
    let (_, offset) = geometry();
    Some((position.x as f64 / scale + offset.0, position.y as f64 / scale + offset.1))
}

fn place(window: &tauri::WebviewWindow, rect: Rect) -> Result<(), String> {
    window.set_size(LogicalSize::new(rect.w, rect.h)).map_err(|e| e.to_string())?;
    window.set_position(LogicalPosition::new(rect.x, rect.y)).map_err(|e| e.to_string())
}

/// Change the widget's shape: "panel", "orb" or "flyout" (with the flyout's measured size). Saves whether it is collapsed.
pub fn set_widget_shape(app: &AppHandle, shape: &str, flyout: Option<(f64, f64)>) -> Result<OrbLayout, String> {
    let window = app.get_webview_window(WIDGET).ok_or("The widget is not open")?;
    let state = app.state::<SettingsState>();
    let mut settings = state.get();
    let (current, _) = geometry();
    let anchor = if current == Shape::Panel {
        orb_anchor(app, &settings.widget).map(|p| (p.x, p.y)).or_else(|| current_anchor(&window)).ok_or("The widget position is unknown")?
    } else {
        current_anchor(&window).ok_or("The widget position is unknown")?
    };
    let area = work_area(app, (anchor.0 + ORB / 2.0, anchor.1 + ORB / 2.0)).unwrap_or(Rect { x: 0.0, y: 0.0, w: 1920.0, h: 1080.0 });
    let layout = match shape {
        "panel" => {
            let size = settings.widget.bounds.map(|b| (b.width, b.height)).unwrap_or(WIDGET_SIZE);
            let rect = panel_window(anchor, size, area);
            set_geometry(Shape::Panel, (0.0, 0.0));
            let _ = window.set_resizable(true);
            place(&window, rect)?;
            let _ = window.set_min_size(Some(LogicalSize::new(WIDGET_MIN.0, WIDGET_MIN.1)));
            OrbLayout { shape: "panel".into(), side: "right".into(), orb_x: 0.0, orb_y: 0.0, width: rect.w, height: rect.h }
        }
        "orb" | "flyout" => {
            let (rect, offset, left) = match (shape, flyout) {
                ("flyout", Some(size)) if size.0.is_finite() && size.1.is_finite() => {
                    flyout_window(anchor, (size.0.clamp(120.0, 480.0), size.1.clamp(40.0, 640.0)), area)
                }
                _ => (Rect { x: anchor.0, y: anchor.1, w: ORB, h: ORB }, (0.0, 0.0), false),
            };
            let _ = window.set_min_size(None::<LogicalSize<f64>>);
            let _ = window.set_resizable(false);
            set_geometry(if rect.w > ORB { Shape::Flyout } else { Shape::Orb }, offset);
            place(&window, rect)?;
            OrbLayout { shape: if rect.w > ORB { "flyout" } else { "orb" }.into(), side: if left { "left" } else { "right" }.into(), orb_x: offset.0, orb_y: offset.1, width: rect.w, height: rect.h }
        }
        _ => return Err("Unknown widget shape".into()),
    };
    let collapsed = shape != "panel";
    let orb = Some(OrbPoint { x: anchor.0, y: anchor.1 }).and_then(OrbPoint::sane);
    if settings.widget.collapsed != collapsed || (collapsed && settings.widget.orb != orb) {
        settings.widget.collapsed = collapsed;
        if collapsed {
            settings.widget.orb = orb;
        }
        if let Ok(saved) = state.save(settings) {
            let _ = app.emit("settings://changed", saved);
        }
    }
    Ok(layout)
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

/// The widget moved or was resized: save its bounds (the circle's position, when it is a circle) half a second after
/// the last change.
pub fn remember_widget_bounds(window: &tauri::Window) {
    let app = window.app_handle().clone();
    if geometry().0 != Shape::Panel {
        let seq = BOUNDS_SEQ.fetch_add(1, Ordering::SeqCst) + 1;
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(500));
            if BOUNDS_SEQ.load(Ordering::SeqCst) != seq || geometry().0 == Shape::Panel {
                return;
            }
            let Some(anchor) = app.get_webview_window(WIDGET).filter(|w| w.is_visible().unwrap_or(false)).and_then(|w| current_anchor(&w)) else { return };
            let state = app.state::<SettingsState>();
            let mut settings = state.get();
            let orb = OrbPoint { x: anchor.0, y: anchor.1 }.sane();
            if orb.is_none() || settings.widget.orb == orb {
                return;
            }
            settings.widget.orb = orb;
            if let Ok(saved) = state.save(settings) {
                let _ = app.emit("settings://changed", saved);
            }
        });
        return;
    }
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
    fn the_flyout_opens_where_there_is_room_and_the_circle_stays_put() {
        let area = Rect { x: 0.0, y: 0.0, w: 1920.0, h: 1040.0 };
        let (rect, offset, left) = flyout_window((100.0, 200.0), (240.0, 180.0), area);
        assert!(!left);
        assert_eq!((rect.x, rect.y, rect.w, rect.h), (100.0, 200.0, ORB + FLYOUT_GAP + 240.0, 180.0));
        assert_eq!(offset, (0.0, 0.0));
        let (rect, offset, left) = flyout_window((1850.0, 200.0), (240.0, 40.0), area);
        assert!(left);
        assert_eq!(rect.x + offset.0, 1850.0);
        assert_eq!(rect.h, ORB);
        let (rect, offset, _) = flyout_window((100.0, 1000.0), (240.0, 300.0), area);
        assert_eq!(rect.y + rect.h, 1040.0);
        assert_eq!(rect.y + offset.1, 1000.0);
        let panel = panel_window((1850.0, 1000.0), (320.0, 540.0), area);
        assert_eq!((panel.x, panel.y), (1920.0 - 320.0, 1040.0 - 540.0));
        assert_eq!(panel_window((900.0, 100.0), (320.0, 540.0), area).x, 900.0 + ORB - 320.0);
        assert_eq!(panel_window((10.0, 10.0), (320.0, 540.0), area).x, 0.0);
    }

    #[test]
    fn pointer_inside_the_frame_only() {
        assert!(contains((100.0, 100.0), (50, 60), (360, 560)));
        assert!(contains((50.0, 60.0), (50, 60), (360, 560)));
        assert!(!contains((410.0, 100.0), (50, 60), (360, 560)));
        assert!(!contains((100.0, 59.5), (50, 60), (360, 560)));
        assert!(contains((-10.0, 5.0), (-100, 0), (200, 100)));
    }
}
