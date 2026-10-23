#!/usr/bin/env python3
import argparse, datetime, json, os, shutil, subprocess, sys, tarfile, tempfile


def get_remote() -> str:
    if not shutil.which("rclone"):
        return ""
    res = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True)
    return next((l.strip().rstrip(":") for l in res.stdout.splitlines() if l.strip().endswith(":")), "")


def rclone_args(folder_id: str) -> list:
    return ["--drive-root-folder-id", folder_id] if folder_id else []


def gdrive_exists(remote: str, folder_id: str, rel_path: str) -> bool:
    if not (remote and folder_id):
        return False
    res = subprocess.run(["rclone", "lsf", f"{remote}:/{rel_path}", *rclone_args(folder_id)], capture_output=True, text=True)
    return res.returncode == 0 and bool(res.stdout.strip())


def gdrive_pull(remote: str, folder_id: str, rel_path: str, local_path: str, is_dir: bool = False) -> bool:
    if not (remote and folder_id):
        return os.path.exists(local_path)
    os.makedirs(local_path if is_dir else os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
    op = "copy" if is_dir else "copyto"
    return subprocess.run(["rclone", op, f"{remote}:/{rel_path}", local_path, *rclone_args(folder_id)], capture_output=True).returncode == 0


def gdrive_push(remote: str, folder_id: str, local_path: str, rel_path: str) -> bool:
    if not (remote and folder_id and os.path.exists(local_path)):
        return False
    return subprocess.run(["rclone", "copyto", local_path, f"{remote}:/{rel_path}", *rclone_args(folder_id)], capture_output=True).returncode == 0


def ensure_engine(remote: str, prod_id: str):
    if os.path.exists("studio/pipelines/line1_presenter_compositor.py"):
        return
    tar_p = "/tmp/engine_bundle.tar.gz"
    if not gdrive_pull(remote, prod_id, "_ENGINE/engine_bundle.tar.gz", tar_p):
        raise RuntimeError("Failed to pull _ENGINE/engine_bundle.tar.gz from PRODUCTION")
    with tarfile.open(tar_p, "r:gz") as tf:
        tf.extractall(".")


def build_contact_sheet(video_path: str, out_jpg: str):
    dur = max(1.0, float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path]).decode().strip() or 4.0))
    beats = [round((i / 7.0) * max(0.5, dur * 0.85), 2) for i in range(8)]
    with tempfile.TemporaryDirectory() as td:
        tiles = []
        for idx, t in enumerate(beats):
            tp = os.path.join(td, f"t{idx}.png")
            subprocess.call(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", video_path, "-frames:v", "1", "-vf", "scale=270:-2", tp])
            if not os.path.exists(tp) and tiles:
                shutil.copy2(tiles[-1], tp)
            tiles.append(tp)
        inputs = [a for tp in tiles for a in ("-i", tp)]
        fl = "".join(f"[{i}:v]" for i in range(8)) + "xstack=inputs=8:layout=0_0|w0_0|w0+w1_0|w0+w1+w2_0|0_h0|w0_h0|w0+w1_h0|w0+w1+w2_h0"
        subprocess.check_call(["ffmpeg", "-loglevel", "error", "-y", *inputs, "-filter_complex", fl, "-frames:v", "1", "-q:v", "3", out_jpg])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ep-id", default=os.environ.get("EP_ID", "T01_EP003_B1"))
    ap.add_argument("--promote-to-root", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    ep_id, prod_id, root_id, remote = args.ep_id, os.environ.get("GDRIVE_PRODUCTION_FOLDER_ID", "").strip(), os.environ.get("GDRIVE_ROOT_FOLDER_ID", "").strip(), get_remote()
    ensure_engine(remote, prod_id)
    sys.path.insert(0, os.getcwd())
    from studio.pipelines.lipsync_flow_single import stitch_single
    from studio.pipelines.lipsync_flow_retime import compose_cover

    wdir = os.path.join("workspace", ep_id)
    os.makedirs(wdir, exist_ok=True)
    final_local, final_remote = os.path.join(wdir, "final.mp4"), f"{ep_id}/final.mp4"
    if not args.force and gdrive_exists(remote, prod_id, final_remote):
        print(f"[SKIP - ALREADY ON GDRIVE] {final_remote}")
        gdrive_pull(remote, prod_id, final_remote, final_local)
        return

    flow_raw = os.path.join(wdir, "cover_flow", "flow_raw.mp4")
    ui_png = os.path.join(wdir, "frames", "ui_overlay.png")
    hook_wav = os.path.join(wdir, "voice", "hook.wav")
    body_raw = os.path.join(wdir, "body_raw.mp4")
    gdrive_pull(remote, prod_id, f"{ep_id}/cover_flow/flow_raw.mp4", flow_raw)
    gdrive_pull(remote, prod_id, f"{ep_id}/frames/ui_overlay.png", ui_png)
    gdrive_pull(remote, prod_id, f"{ep_id}/voice/hook.wav", hook_wav)
    gdrive_pull(remote, prod_id, f"{ep_id}/body_raw.mp4", body_raw)

    cov3d_local, stitched_local = os.path.join(wdir, "cover_3d.mp4"), os.path.join(wdir, "stitched_raw.mp4")
    hook_v, hook_a = os.path.join(wdir, "hook_v.mp4"), os.path.join(wdir, "hook_a.wav")
    meta = stitch_single(flow_raw, hook_wav, hook_v, hook_a)
    compose_cover(hook_v, hook_a, ui_png, body_raw, cov3d_local, stitched_local, clip_dur=float(meta["clip_dur"]) - 0.02)
    print(f"[STAGE 1 SAVED TO GDRIVE={gdrive_push(remote, prod_id, cov3d_local, f'{ep_id}/cover_3d.mp4')}] {ep_id}/cover_3d.mp4")

    subprocess.check_call(["ffmpeg", "-y", "-i", stitched_local, "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "24000", "-ac", "1", "-movflags", "+faststart", final_local], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[STAGE 2 SAVED TO GDRIVE={gdrive_push(remote, prod_id, final_local, final_remote)}] {final_remote}")

    qc_local, cov_jpg = os.path.join(wdir, "mirror_qc.jpg"), os.path.join(wdir, "Cover.png")
    build_contact_sheet(final_local, qc_local)
    subprocess.check_call(["ffmpeg", "-loglevel", "error", "-y", "-ss", "0.8", "-i", cov3d_local, "-frames:v", "1", cov_jpg])
    gdrive_push(remote, prod_id, qc_local, f"{ep_id}/mirror_qc.jpg")
    gdrive_push(remote, prod_id, cov_jpg, f"{ep_id}/Cover.png")
    if args.promote_to_root and root_id:
        gdrive_push(remote, root_id, final_local, f"{ep_id}.mp4")
    json.dump({"ep_id": ep_id, "stages": ["cover_3d.mp4", "final.mp4", "Cover.png", "mirror_qc.jpg"], "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}, open(os.path.join(wdir, "master_checkpoint.json"), "w"), indent=2)
    gdrive_push(remote, prod_id, os.path.join(wdir, "master_checkpoint.json"), f"{ep_id}/master_checkpoint.json")
    print(f"[PASS] harien.mpeg completed Live 3D Mascot Cover + xfade + -14 LUFS -> {final_local}")


if __name__ == "__main__":
    main()
