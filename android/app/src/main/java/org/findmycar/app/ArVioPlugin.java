package org.findmycar.app;

import android.Manifest;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

/**
 * Capacitor bridge for ARCore 6-DOF poses.
 * JS: Capacitor.Plugins.ArVio.start() / getPose() / stop()
 */
@CapacitorPlugin(
    name = "ArVio",
    permissions = {
        @Permission(
            alias = "camera",
            strings = { Manifest.permission.CAMERA }
        )
    }
)
public class ArVioPlugin extends Plugin {
    private ArVioSession arSession;

    @PluginMethod
    public void isAvailable(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("available", ArVioSession.isArCoreSupported(getContext()));
        call.resolve(ret);
    }

    @PluginMethod
    public void start(PluginCall call) {
        if (getPermissionState("camera") != com.getcapacitor.PermissionState.GRANTED) {
            requestPermissionForAlias("camera", call, "cameraPermResult");
            return;
        }
        startSession(call);
    }

    @PermissionCallback
    private void cameraPermResult(PluginCall call) {
        if (getPermissionState("camera") == com.getcapacitor.PermissionState.GRANTED) {
            startSession(call);
        } else {
            call.reject("camera permission denied");
        }
    }

    private void startSession(PluginCall call) {
        try {
            if (arSession == null) {
                arSession = new ArVioSession(getContext());
            }
            arSession.start();
            JSObject ret = new JSObject();
            ret.put("started", true);
            ret.put("available", true);
            call.resolve(ret);
        } catch (Exception e) {
            call.reject("ARCore start failed: " + e.getMessage(), e);
        }
    }

    @PluginMethod
    public void stop(PluginCall call) {
        if (arSession != null) {
            arSession.stop();
        }
        JSObject ret = new JSObject();
        ret.put("stopped", true);
        call.resolve(ret);
    }

    @PluginMethod
    public void getPose(PluginCall call) {
        if (arSession == null || !arSession.isRunning()) {
            JSObject ret = new JSObject();
            ret.put("tracking", "stopped");
            ret.put("x", 0.0);
            ret.put("y", 0.0);
            ret.put("z", 0.0);
            ret.put("qw", 1.0);
            ret.put("qx", 0.0);
            ret.put("qy", 0.0);
            ret.put("qz", 0.0);
            ret.put("headingDeg", 0.0);
            call.resolve(ret);
            return;
        }
        call.resolve(arSession.latestPoseJson());
    }

    /** latest low-res preview JPEG (non-blocking). */
    @PluginMethod
    public void getPreview(PluginCall call) {
        if (arSession == null || !arSession.isRunning()) {
            JSObject ret = new JSObject();
            ret.put("ok", false);
            call.resolve(ret);
            return;
        }
        call.resolve(arSession.latestPreviewJson());
    }

    /** grab one JPEG from ARCore camera (not WebView getUserMedia). */
    @PluginMethod
    public void captureFrame(PluginCall call) {
        if (arSession == null || !arSession.isRunning()) {
            call.reject("ARCore not running");
            return;
        }
        // block off the bridge thread — captureJpeg waits for next AR frame
        new Thread(() -> {
            try {
                JSObject ret = arSession.captureJpeg(2500);
                if (Boolean.TRUE.equals(ret.getBool("ok"))) {
                    call.resolve(ret);
                } else {
                    String err = ret.getString("error");
                    call.reject(err != null ? err : "capture failed");
                }
            } catch (Exception e) {
                call.reject("capture failed: " + e.getMessage(), e);
            }
        }, "ar-capture").start();
    }

    @Override
    protected void handleOnDestroy() {
        if (arSession != null) {
            arSession.stop();
            arSession = null;
        }
        super.handleOnDestroy();
    }
}
