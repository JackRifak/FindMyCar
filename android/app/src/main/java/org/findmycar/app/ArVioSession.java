package org.findmycar.app;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.ImageFormat;
import android.graphics.Matrix;
import android.graphics.Rect;
import android.graphics.SurfaceTexture;
import android.graphics.YuvImage;
import android.media.Image;
import android.opengl.EGL14;
import android.opengl.EGLConfig;
import android.opengl.EGLContext;
import android.opengl.EGLDisplay;
import android.opengl.EGLSurface;
import android.opengl.GLES20;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Base64;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Surface;
import android.view.WindowManager;

import com.getcapacitor.JSObject;
import com.google.ar.core.ArCoreApk;
import com.google.ar.core.Camera;
import com.google.ar.core.CameraConfig;
import com.google.ar.core.CameraConfigFilter;
import com.google.ar.core.CameraIntrinsics;
import com.google.ar.core.Frame;
import com.google.ar.core.Pose;
import com.google.ar.core.Session;
import com.google.ar.core.TrackingState;
import com.google.ar.core.exceptions.CameraNotAvailableException;
import com.google.ar.core.exceptions.UnavailableException;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.util.EnumSet;
import java.util.List;

/**
 * Headless ARCore session: EGL + camera texture, pose + JPEG capture on a worker thread.
 * Positions are ARCore world meters (Y-up). Quaternion is camera orientation.
 */
public class ArVioSession {
    private static final String TAG = "ArVioSession";
    private static final int UPDATE_MS = 33;
    private static final int JPEG_QUALITY = 95;
    /** preferred ARCore CPU image size (long x short side) for mapping / PnP */
    private static final int TARGET_CPU_W = 1280;
    private static final int TARGET_CPU_H = 720;

    private final Context context;
    private final Object lock = new Object();
    private final Object captureLock = new Object();

    private Session session;
    private HandlerThread worker;
    private Handler handler;
    private boolean running;

    private EGLDisplay eglDisplay = EGL14.EGL_NO_DISPLAY;
    private EGLContext eglContext = EGL14.EGL_NO_CONTEXT;
    private EGLSurface eglSurface = EGL14.EGL_NO_SURFACE;
    private int cameraTextureId = -1;
    private SurfaceTexture surfaceTexture;

    private double x, y, z;
    private double qw = 1, qx, qy, qz;
    private double headingDeg;
    private String tracking = "initializing";
    private long timestampMs;

    private boolean captureRequested;
    private String captureBase64;
    private int captureW;
    private int captureH;
    private String captureErr;
    // intrinsics of the captured JPEG (after display rotation), pixels: fx, fy, cx, cy
    private float[] captureIntr;
    // ARCore pose of the exact frame in the captured JPEG (same convention as latestPoseJson)
    private JSObject capturePose;

    // rolling preview (~8–10 fps) — separate from hi-res keyframe grabs
    private String previewBase64;
    private int previewW;
    private int previewH;
    private long lastPreviewMs;
    private static final int PREVIEW_MIN_MS = 100;
    private static final int PREVIEW_QUALITY = 55;

    public ArVioSession(Context context) {
        this.context = context.getApplicationContext();
    }

    public static boolean isArCoreSupported(Context ctx) {
        try {
            ArCoreApk.Availability avail = ArCoreApk.getInstance().checkAvailability(ctx);
            return avail == ArCoreApk.Availability.SUPPORTED_INSTALLED
                || avail == ArCoreApk.Availability.SUPPORTED_APK_TOO_OLD
                || avail == ArCoreApk.Availability.SUPPORTED_NOT_INSTALLED;
        } catch (Throwable t) {
            return false;
        }
    }

    public boolean isRunning() {
        return running;
    }

    public void start() throws Exception {
        synchronized (lock) {
            if (running) return;

            ArCoreApk.Availability avail = ArCoreApk.getInstance().checkAvailability(context);
            if (avail == ArCoreApk.Availability.UNSUPPORTED_DEVICE_NOT_CAPABLE) {
                throw new UnavailableException("device does not support ARCore");
            }
            session = new Session(context);
            selectCpuImageConfig();
            com.google.ar.core.Config cfg = new com.google.ar.core.Config(session);
            cfg.setUpdateMode(com.google.ar.core.Config.UpdateMode.LATEST_CAMERA_IMAGE);
            cfg.setFocusMode(com.google.ar.core.Config.FocusMode.AUTO);
            session.configure(cfg);

            worker = new HandlerThread("ar-vio");
            worker.start();
            handler = new Handler(worker.getLooper());
            running = true;
            tracking = "initializing";

            handler.post(this::setupGlAndLoop);
        }
    }

    public void stop() {
        synchronized (lock) {
            running = false;
            if (handler != null) {
                handler.removeCallbacksAndMessages(null);
                handler.post(this::teardownGl);
            }
            if (worker != null) {
                worker.quitSafely();
                worker = null;
                handler = null;
            }
            if (session != null) {
                try {
                    session.pause();
                    session.close();
                } catch (Throwable t) {
                    Log.w(TAG, "session close", t);
                }
                session = null;
            }
            tracking = "stopped";
            previewBase64 = null;
        }
        synchronized (captureLock) {
            captureRequested = false;
            captureBase64 = null;
            captureErr = "stopped";
            captureLock.notifyAll();
        }
    }

    public JSObject latestPoseJson() {
        JSObject ret = new JSObject();
        synchronized (lock) {
            ret.put("x", x);
            ret.put("y", y);
            ret.put("z", z);
            ret.put("qw", qw);
            ret.put("qx", qx);
            ret.put("qy", qy);
            ret.put("qz", qz);
            ret.put("headingDeg", headingDeg);
            ret.put("tracking", tracking);
            ret.put("timestamp", timestampMs / 1000.0);
        }
        return ret;
    }

    /** latest preview JPEG if any (non-blocking). */
    public JSObject latestPreviewJson() {
        JSObject ret = new JSObject();
        synchronized (lock) {
            if (previewBase64 == null) {
                ret.put("ok", false);
                return ret;
            }
            ret.put("ok", true);
            ret.put("jpegBase64", previewBase64);
            ret.put("width", previewW);
            ret.put("height", previewH);
        }
        return ret;
    }

    /** block until next hi-res AR frame JPEG is ready (or timeout). */
    public JSObject captureJpeg(long timeoutMs) throws InterruptedException {
        synchronized (captureLock) {
            captureRequested = true;
            captureBase64 = null;
            captureErr = null;
            captureW = 0;
            captureH = 0;
            captureIntr = null;
            capturePose = null;
            captureLock.wait(timeoutMs);
            JSObject ret = new JSObject();
            if (captureBase64 != null) {
                ret.put("ok", true);
                ret.put("jpegBase64", captureBase64);
                ret.put("width", captureW);
                ret.put("height", captureH);
                if (capturePose != null) {
                    ret.put("pose", capturePose);
                }
                if (captureIntr != null) {
                    ret.put("fx", captureIntr[0]);
                    ret.put("fy", captureIntr[1]);
                    ret.put("cx", captureIntr[2]);
                    ret.put("cy", captureIntr[3]);
                }
            } else {
                ret.put("ok", false);
                ret.put("error", captureErr != null ? captureErr : "timeout");
            }
            return ret;
        }
    }

    private void setupGlAndLoop() {
        try {
            initEgl();
            int[] tex = new int[1];
            GLES20.glGenTextures(1, tex, 0);
            cameraTextureId = tex[0];
            GLES20.glBindTexture(android.opengl.GLES11Ext.GL_TEXTURE_EXTERNAL_OES, cameraTextureId);
            GLES20.glTexParameteri(android.opengl.GLES11Ext.GL_TEXTURE_EXTERNAL_OES,
                GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE);
            GLES20.glTexParameteri(android.opengl.GLES11Ext.GL_TEXTURE_EXTERNAL_OES,
                GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE);
            GLES20.glTexParameteri(android.opengl.GLES11Ext.GL_TEXTURE_EXTERNAL_OES,
                GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR);
            GLES20.glTexParameteri(android.opengl.GLES11Ext.GL_TEXTURE_EXTERNAL_OES,
                GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR);

            surfaceTexture = new SurfaceTexture(cameraTextureId);
            session.setCameraTextureName(cameraTextureId);
            applyDisplayGeometry();
            session.resume();
            handler.post(this::updateOnce);
        } catch (Throwable t) {
            Log.e(TAG, "GL/ARCore setup failed", t);
            synchronized (lock) {
                tracking = "lost";
                running = false;
            }
        }
    }

    private void applyDisplayGeometry() {
        try {
            WindowManager wm = (WindowManager) context.getSystemService(Context.WINDOW_SERVICE);
            int rot = wm != null ? wm.getDefaultDisplay().getRotation() : Surface.ROTATION_0;
            DisplayMetrics dm = context.getResources().getDisplayMetrics();
            session.setDisplayGeometry(rot, dm.widthPixels, dm.heightPixels);
        } catch (Throwable t) {
            Log.w(TAG, "setDisplayGeometry", t);
        }
    }

    private void updateOnce() {
        if (!running || session == null) return;
        try {
            if (surfaceTexture != null) {
                surfaceTexture.updateTexImage();
            }
            applyDisplayGeometry();
            Frame frame = session.update();
            Camera camera = frame.getCamera();
            TrackingState state = camera.getTrackingState();
            String status;
            if (state == TrackingState.TRACKING) {
                status = "tracking";
            } else if (state == TrackingState.PAUSED) {
                status = "initializing";
            } else {
                status = "lost";
            }

            Pose pose = camera.getDisplayOrientedPose();
            float[] t = pose.getTranslation();
            float[] q = pose.getRotationQuaternion(); // qx,qy,qz,qw

            double px = t[0];
            double py = t[1];
            double pz = -t[2];

            float[] fwd = pose.getZAxis();
            double fx = fwd[0];
            double fz = -fwd[2];
            double heading = Math.toDegrees(Math.atan2(fx, fz));
            if (heading < 0) heading += 360.0;

            synchronized (lock) {
                x = px;
                y = py;
                z = pz;
                qx = q[0];
                qy = q[1];
                qz = q[2];
                qw = q[3];
                headingDeg = heading;
                tracking = status;
                timestampMs = System.currentTimeMillis();
            }

            boolean wantCapture;
            synchronized (captureLock) {
                wantCapture = captureRequested;
            }
            long now = System.currentTimeMillis();
            boolean wantPreview = (now - lastPreviewMs) >= PREVIEW_MIN_MS;
            if (wantCapture || wantPreview) {
                grabJpeg(frame, wantCapture, wantPreview);
            }
        } catch (CameraNotAvailableException e) {
            Log.w(TAG, "camera not available", e);
            synchronized (lock) {
                tracking = "lost";
            }
            failCapture("camera not available");
        } catch (Throwable t) {
            Log.w(TAG, "update failed", t);
            failCapture(t.getMessage() != null ? t.getMessage() : "update failed");
        }
        if (running && handler != null) {
            handler.postDelayed(this::updateOnce, UPDATE_MS);
        }
    }

    private void grabJpeg(Frame frame, boolean hiRes, boolean preview) {
        Image image = null;
        try {
            image = frame.acquireCameraImage();
            byte[] nv21 = yuv420ToNv21(image);
            int w = image.getWidth();
            int h = image.getHeight();
            YuvImage yuv = new YuvImage(nv21, ImageFormat.NV21, w, h, null);

            byte[] previewJpeg = null;
            byte[] keyJpeg = null;
            float[] keyIntr = null;
            // updateOnce stored this frame's pose just before calling us — snapshot it now
            JSObject keyPose = hiRes ? latestPoseJson() : null;
            if (preview) {
                ByteArrayOutputStream baos = new ByteArrayOutputStream();
                yuv.compressToJpeg(new Rect(0, 0, w, h), PREVIEW_QUALITY, baos);
                previewJpeg = rotateJpegForDisplay(baos.toByteArray());
            }
            if (hiRes) {
                ByteArrayOutputStream baos = new ByteArrayOutputStream();
                // near-lossless first pass when rotateJpegForDisplay re-encodes at JPEG_QUALITY
                yuv.compressToJpeg(new Rect(0, 0, w, h), displayRotationDeg() == 0 ? JPEG_QUALITY : 100, baos);
                keyJpeg = rotateJpegForDisplay(baos.toByteArray());
                keyIntr = rotatedIntrinsics(frame.getCamera().getImageIntrinsics(), w, h);
            } else if (previewJpeg != null) {
                // reuse preview bytes for dimensions if only preview
                keyJpeg = null;
            }

            if (previewJpeg != null) {
                BitmapFactory.Options opts = new BitmapFactory.Options();
                opts.inJustDecodeBounds = true;
                BitmapFactory.decodeByteArray(previewJpeg, 0, previewJpeg.length, opts);
                synchronized (lock) {
                    previewBase64 = Base64.encodeToString(previewJpeg, Base64.NO_WRAP);
                    previewW = opts.outWidth > 0 ? opts.outWidth : w;
                    previewH = opts.outHeight > 0 ? opts.outHeight : h;
                    lastPreviewMs = System.currentTimeMillis();
                }
            }

            if (hiRes && keyJpeg != null) {
                BitmapFactory.Options opts = new BitmapFactory.Options();
                opts.inJustDecodeBounds = true;
                BitmapFactory.decodeByteArray(keyJpeg, 0, keyJpeg.length, opts);
                synchronized (captureLock) {
                    captureBase64 = Base64.encodeToString(keyJpeg, Base64.NO_WRAP);
                    captureW = opts.outWidth > 0 ? opts.outWidth : w;
                    captureH = opts.outHeight > 0 ? opts.outHeight : h;
                    captureIntr = keyIntr;
                    capturePose = keyPose;
                    captureErr = null;
                    captureRequested = false;
                    captureLock.notifyAll();
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "capture jpeg failed", t);
            failCapture(t.getMessage() != null ? t.getMessage() : "capture failed");
        } finally {
            if (image != null) {
                image.close();
            }
        }
    }

    private void failCapture(String msg) {
        synchronized (captureLock) {
            if (!captureRequested) return;
            captureErr = msg;
            captureBase64 = null;
            captureRequested = false;
            captureLock.notifyAll();
        }
    }

    /** clockwise degrees the sensor image is rotated to match the display */
    private int displayRotationDeg() {
        WindowManager wm = (WindowManager) context.getSystemService(Context.WINDOW_SERVICE);
        int rot = wm != null ? wm.getDefaultDisplay().getRotation() : Surface.ROTATION_0;
        if (rot == Surface.ROTATION_0) return 90;
        if (rot == Surface.ROTATION_180) return 270;
        if (rot == Surface.ROTATION_270) return 180;
        return 0;
    }

    /**
     * ARCore CPU-image intrinsics (sensor orientation, w x h) mapped onto the JPEG produced by
     * rotateJpegForDisplay. Returns {fx, fy, cx, cy} in pixels, or null.
     */
    private float[] rotatedIntrinsics(CameraIntrinsics intr, int w, int h) {
        try {
            float[] f = intr.getFocalLength();
            float[] c = intr.getPrincipalPoint();
            int[] dims = intr.getImageDimensions();
            // intrinsics belong to the CPU image; rescale if dims ever differ from the buffer
            float sx = dims[0] > 0 ? (float) w / dims[0] : 1f;
            float sy = dims[1] > 0 ? (float) h / dims[1] : 1f;
            float fx = f[0] * sx, fy = f[1] * sy, cx = c[0] * sx, cy = c[1] * sy;
            switch (displayRotationDeg()) {
                case 90: // clockwise: (u, v) -> (h - v, u)
                    return new float[] { fy, fx, h - cy, cx };
                case 180: // (u, v) -> (w - u, h - v)
                    return new float[] { fx, fy, w - cx, h - cy };
                case 270: // (u, v) -> (v, w - u)
                    return new float[] { fy, fx, cy, w - cx };
                default:
                    return new float[] { fx, fy, cx, cy };
            }
        } catch (Throwable t) {
            Log.w(TAG, "intrinsics", t);
            return null;
        }
    }

    /** pick the back-camera config whose CPU image is closest to TARGET_CPU_W x TARGET_CPU_H */
    private void selectCpuImageConfig() {
        try {
            CameraConfigFilter filter = new CameraConfigFilter(session)
                .setFacingDirection(CameraConfig.FacingDirection.BACK)
                .setTargetFps(EnumSet.of(CameraConfig.TargetFps.TARGET_FPS_30));
            List<CameraConfig> configs = session.getSupportedCameraConfigs(filter);
            CameraConfig best = null;
            long bestScore = Long.MAX_VALUE;
            for (CameraConfig cc : configs) {
                android.util.Size sz = cc.getImageSize();
                int lw = Math.max(sz.getWidth(), sz.getHeight());
                int sh = Math.min(sz.getWidth(), sz.getHeight());
                long score = Math.abs(lw - TARGET_CPU_W) + Math.abs(sh - TARGET_CPU_H);
                // smaller than target only if nothing at or above it exists
                if (lw < TARGET_CPU_W) score += 10000;
                if (score < bestScore) {
                    bestScore = score;
                    best = cc;
                }
            }
            if (best != null) {
                session.setCameraConfig(best);
                Log.i(TAG, "CPU image config " + best.getImageSize() + " (of " + configs.size() + ")");
            }
        } catch (Throwable t) {
            Log.w(TAG, "camera config select failed, using ARCore default CPU image", t);
        }
    }

    private byte[] rotateJpegForDisplay(byte[] jpeg) {
        try {
            int deg = displayRotationDeg();
            if (deg == 0) return jpeg;

            Bitmap bmp = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length);
            if (bmp == null) return jpeg;
            Matrix m = new Matrix();
            m.postRotate(deg);
            Bitmap rotated = Bitmap.createBitmap(bmp, 0, 0, bmp.getWidth(), bmp.getHeight(), m, true);
            bmp.recycle();
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            rotated.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out);
            rotated.recycle();
            return out.toByteArray();
        } catch (Throwable t) {
            Log.w(TAG, "rotate jpeg", t);
            return jpeg;
        }
    }

    private static byte[] yuv420ToNv21(Image image) {
        int w = image.getWidth();
        int h = image.getHeight();
        Image.Plane[] planes = image.getPlanes();
        ByteBuffer yBuf = planes[0].getBuffer();
        ByteBuffer uBuf = planes[1].getBuffer();
        ByteBuffer vBuf = planes[2].getBuffer();
        int yRow = planes[0].getRowStride();
        int uvRow = planes[1].getRowStride();
        int uvPix = planes[1].getPixelStride();

        byte[] nv21 = new byte[w * h * 3 / 2];
        int pos = 0;
        for (int row = 0; row < h; row++) {
            yBuf.position(row * yRow);
            yBuf.get(nv21, pos, w);
            pos += w;
        }

        int uvH = h / 2;
        int uvW = w / 2;
        byte[] uBytes = new byte[uvRow];
        byte[] vBytes = new byte[uvRow];
        for (int row = 0; row < uvH; row++) {
            uBuf.position(row * uvRow);
            vBuf.position(row * uvRow);
            int uLen = Math.min(uvRow, uBuf.remaining());
            int vLen = Math.min(uvRow, vBuf.remaining());
            uBuf.get(uBytes, 0, uLen);
            vBuf.get(vBytes, 0, vLen);
            for (int col = 0; col < uvW; col++) {
                int ui = col * uvPix;
                int vi = col * uvPix;
                nv21[pos++] = vBytes[vi];
                nv21[pos++] = uBytes[ui];
            }
        }
        return nv21;
    }

    private void initEgl() {
        eglDisplay = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY);
        int[] ver = new int[2];
        EGL14.eglInitialize(eglDisplay, ver, 0, ver, 1);
        int[] attribList = {
            EGL14.EGL_RED_SIZE, 8,
            EGL14.EGL_GREEN_SIZE, 8,
            EGL14.EGL_BLUE_SIZE, 8,
            EGL14.EGL_ALPHA_SIZE, 8,
            EGL14.EGL_RENDERABLE_TYPE, EGL14.EGL_OPENGL_ES2_BIT,
            EGL14.EGL_SURFACE_TYPE, EGL14.EGL_PBUFFER_BIT,
            EGL14.EGL_NONE
        };
        EGLConfig[] configs = new EGLConfig[1];
        int[] numConfig = new int[1];
        EGL14.eglChooseConfig(eglDisplay, attribList, 0, configs, 0, 1, numConfig, 0);
        int[] ctxAttrib = { EGL14.EGL_CONTEXT_CLIENT_VERSION, 2, EGL14.EGL_NONE };
        eglContext = EGL14.eglCreateContext(eglDisplay, configs[0], EGL14.EGL_NO_CONTEXT, ctxAttrib, 0);
        int[] surfAttrib = { EGL14.EGL_WIDTH, 1, EGL14.EGL_HEIGHT, 1, EGL14.EGL_NONE };
        eglSurface = EGL14.eglCreatePbufferSurface(eglDisplay, configs[0], surfAttrib, 0);
        EGL14.eglMakeCurrent(eglDisplay, eglSurface, eglSurface, eglContext);
    }

    private void teardownGl() {
        try {
            if (surfaceTexture != null) {
                surfaceTexture.release();
                surfaceTexture = null;
            }
            if (cameraTextureId >= 0) {
                int[] tex = { cameraTextureId };
                GLES20.glDeleteTextures(1, tex, 0);
                cameraTextureId = -1;
            }
            if (eglDisplay != EGL14.EGL_NO_DISPLAY) {
                EGL14.eglMakeCurrent(eglDisplay, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_CONTEXT);
                if (eglSurface != EGL14.EGL_NO_SURFACE) {
                    EGL14.eglDestroySurface(eglDisplay, eglSurface);
                }
                if (eglContext != EGL14.EGL_NO_CONTEXT) {
                    EGL14.eglDestroyContext(eglDisplay, eglContext);
                }
                EGL14.eglTerminate(eglDisplay);
            }
        } catch (Throwable t) {
            Log.w(TAG, "EGL teardown", t);
        }
        eglDisplay = EGL14.EGL_NO_DISPLAY;
        eglContext = EGL14.EGL_NO_CONTEXT;
        eglSurface = EGL14.EGL_NO_SURFACE;
    }
}
