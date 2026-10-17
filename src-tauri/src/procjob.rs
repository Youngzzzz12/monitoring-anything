//! Child processes (the collector, live updates, agent runs) end with the app, even when the app is force-quit.
//!
//! Windows: each child is assigned to one job object that kills its processes when its last handle closes; the app holds
//! that handle until it exits, however it exits, and the children's own children (an agent CLI) join the job too. Only
//! the children are assigned, not the app itself, so an update installer the app starts is not ended with it.
//! macOS and Linux: the live-update loop checks that its parent is still the app (RD_PARENT_PID, see cli.py watch) and
//! stops otherwise; one-off commands end on their own time limits.

use std::process::Child;

#[cfg(windows)]
mod imp {
    use std::os::windows::io::AsRawHandle;
    use std::sync::OnceLock;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    struct Job(HANDLE);
    // The handle is only passed to Win32 calls, which are thread-safe for job objects.
    unsafe impl Send for Job {}
    unsafe impl Sync for Job {}

    static JOB: OnceLock<Option<Job>> = OnceLock::new();

    pub fn job() -> Option<HANDLE> {
        JOB.get_or_init(|| unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return None;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let ok = SetInformationJobObject(handle, JobObjectExtendedLimitInformation, &info as *const _ as *const core::ffi::c_void,
                                             std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32);
            if ok == 0 {
                CloseHandle(handle);
                return None;
            }
            Some(Job(handle))
        }).as_ref().map(|job| job.0)
    }

    pub fn contain(child: &std::process::Child) {
        if let Some(job) = job() {
            unsafe { AssignProcessToJobObject(job, child.as_raw_handle() as HANDLE) };
        }
    }
}

/// Make `child` end with the app (Windows: the app's job object; elsewhere see the module notes).
pub fn contain(child: &Child) {
    #[cfg(windows)]
    imp::contain(child);
    #[cfg(not(windows))]
    let _ = child;
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::System::JobObjects::IsProcessInJob;

    #[test]
    fn children_join_the_job_that_ends_with_the_app() {
        let mut child = std::process::Command::new("cmd").args(["/C", "ping -n 30 127.0.0.1 >NUL"]).spawn().unwrap();
        contain(&child);
        let mut inside = 0;
        unsafe { IsProcessInJob(child.as_raw_handle() as _, imp::job().unwrap(), &mut inside) };
        let _ = child.kill();
        let _ = child.wait();
        assert_ne!(inside, 0);
    }
}
