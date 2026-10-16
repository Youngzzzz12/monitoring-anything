//! System tray: Open dashboard, Show widget, Live updates and Quit.

use std::sync::Mutex;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, Wry};

use crate::collector::CollectorState;

pub struct TrayState {
    widget: CheckMenuItem<Wry>,
    live: CheckMenuItem<Wry>,
}

pub struct TrayHolder(pub Mutex<Option<TrayState>>);

pub fn create(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open dashboard", true, None::<&str>)?;
    let widget = CheckMenuItem::with_id(app, "widget", "Show widget", true, false, None::<&str>)?;
    let live = CheckMenuItem::with_id(app, "live", "Live updates", true, false, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Monitoring Anything", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &widget, &live, &PredefinedMenuItem::separator(app)?, &quit])?;
    let mut builder = TrayIconBuilder::with_id("main")
        .tooltip("Monitoring Anything")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => crate::windows::show_main(app),
            "widget" => {
                // Off the event-loop thread: window creation must not block the main thread that serves it.
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    let visible = crate::windows::widget_visible(&app);
                    if let Err(error) = crate::windows::set_widget_visible(&app, !visible) {
                        let _ = tauri::Emitter::emit(&app, "app://error", error);
                    }
                });
            }
            "live" => {
                let app = app.clone();
                tauri::async_runtime::spawn_blocking(move || {
                    let state = app.state::<std::sync::Arc<CollectorState>>().inner().clone();
                    let result = if state.is_running() {
                        state.stop_watch(&app).map(|_| ())
                    } else {
                        crate::runner(&app).and_then(|runner| state.start_watch(&app, &runner).map(|_| ()))
                    };
                    if let Err(error) = result {
                        let _ = tauri::Emitter::emit(&app, "app://error", error);
                    }
                    sync(&app);
                });
            }
            "quit" => crate::quit(app),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                crate::windows::show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    if let Some(holder) = app.try_state::<TrayHolder>() {
        if let Ok(mut guard) = holder.0.lock() {
            *guard = Some(TrayState { widget, live });
        }
    }
    sync(app);
    Ok(())
}

/// Reflect widget visibility and the live-update state in the check items.
pub fn sync(app: &AppHandle) {
    let Some(holder) = app.try_state::<TrayHolder>() else { return };
    let Ok(guard) = holder.0.lock() else { return };
    let Some(state) = guard.as_ref() else { return };
    let running = app.try_state::<std::sync::Arc<CollectorState>>().map(|s| s.is_running()).unwrap_or(false);
    let _ = state.widget.set_checked(crate::windows::widget_visible(app));
    let _ = state.live.set_checked(running);
}
