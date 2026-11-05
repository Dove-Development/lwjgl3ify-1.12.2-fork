#!/usr/bin/env python3
"""
Launches the built Prism Launcher / MultiMC instance (build/distributions/lwjgl3ify-*-multimc.zip) the way Prism
would, on modern Java, with the production mod jar and MixinBooter in a real mods/ folder, to check a build outside
the development environment. Every library listed in the instance metadata is downloaded and its SHA-1 verified, so
this also validates prism-libraries/ and the generated LWJGL list.

It waits for Forge to finish loading, lets the game run for --run-seconds more (the main menu), and passes if the game
was still running then, with no crash report.

Usage:
  ./gradlew assemble
  python tools/run-production-test.py [--java PATH] [--assets DIR] [--work DIR] [--run-seconds N]
"""

import argparse
import glob
import hashlib
import json
import os
import platform
import queue
import threading
import time
import shutil
import subprocess
import sys
import urllib.request
import uuid
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MC_VERSION = "1.12.2"
FORGE_MAVEN = "https://maven.minecraftforge.net/"
MOJANG_LIBRARIES = "https://libraries.minecraft.net/"
VERSION_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
# MixinBooter, which lwjgl3ify for 1.12.2 needs (Modrinth, hash pinned)
MIXINBOOTER = ("https://cdn.modrinth.com/data/{project}/versions/{version}/{file}", "d0a05c13398fb0ff85e7b41dc6eb15ac69210a1b")
LOADED = "Forge Mod Loader has successfully loaded"


def current_os():
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "osx"
    return "linux"


def current_arch():
    """Architecture names as Prism Launcher maps them (RuntimeContext::mappedJavaRealArchitecture)."""
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x86_64"
    if machine in ("i386", "i686", "x86"):
        return "x86"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine.startswith("arm"):
        return "arm32"
    return machine


def os_rule_matches(name):
    """Prism Launcher's RuntimeContext::classifierMatches: "<os>-<arch>" always, a bare "<os>" only on x86/x86_64."""
    if name == current_os() + "-" + current_arch():
        return True
    return current_arch() in ("x86_64", "x86") and name == current_os()


def rules_allow(rules):
    if not rules:
        return True
    allowed = False
    for rule in rules:
        matches = True
        if "os" in rule:
            want = rule["os"].get("name")
            if want is not None and not os_rule_matches(want):
                matches = False
            if rule["os"].get("arch") == "x86" and platform.architecture()[0] != "32bit":
                matches = False
        if "features" in rule:
            matches = False
        if matches:
            allowed = rule["action"] == "allow"
    return allowed


def maven_path(name):
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = "-" + parts[3] if len(parts) > 3 else ""
    return f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}{classifier}.jar"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "lwjgl3ify-production-test"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def download(url, target, sha1):
    if os.path.isfile(target):
        with open(target, "rb") as f:
            if sha1 is None or hashlib.sha1(f.read()).hexdigest() == sha1:
                return target
    data = fetch(url)
    actual = hashlib.sha1(data).hexdigest()
    if sha1 is not None and actual != sha1:
        raise RuntimeError(f"SHA-1 mismatch for {url}: metadata says {sha1}, got {actual}")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(data)
    return target


def resolve_libraries(libs, libraries, natives, local_dir=None):
    """Downloads launcher-format library entries; returns classpath jars and extracts natives."""
    classpath = []
    arch_bits = "64" if sys.maxsize > 2**32 else "32"
    for lib in libs:
        if not rules_allow(lib.get("rules")):
            continue
        name = lib["name"]
        if lib.get("MMC-hint") == "local":
            local = os.path.join(local_dir, os.path.basename(maven_path(name)))
            if not os.path.isfile(local):
                raise RuntimeError(f"Local library {local} missing from the instance zip")
            classpath.append(local)
            continue
        downloads = lib.get("downloads", {})
        if "natives" in lib:
            classifier = lib["natives"].get(current_os())
            if classifier is not None:
                classifier = classifier.replace("${arch}", arch_bits)
                art = downloads["classifiers"][classifier]
                jar = download(art["url"], os.path.join(libraries, maven_path(name + ":" + classifier)), art.get("sha1"))
                excludes = lib.get("extract", {}).get("exclude", [])
                with zipfile.ZipFile(jar) as z:
                    for entry in z.namelist():
                        if not entry.endswith("/") and not any(entry.startswith(e) for e in excludes):
                            z.extract(entry, natives)
        if "artifact" in downloads:
            art = downloads["artifact"]
            classpath.append(download(art["url"], os.path.join(libraries, maven_path(name)), art.get("sha1")))
        elif "url" in lib:
            classpath.append(download(lib["url"] + maven_path(name), os.path.join(libraries, maven_path(name)), None))
    return classpath


def game_arguments(mc_args, game, assets, asset_index, tweakers):
    index_name = asset_index
    if not os.path.isfile(os.path.join(assets, "indexes", index_name + ".json")):
        # loom names its copies of the index "<minecraft>-<index>"
        candidates = glob.glob(os.path.join(assets, "indexes", "*" + index_name + ".json"))
        if not candidates:
            sys.exit(f"No {index_name} asset index in {assets}")
        index_name = os.path.basename(candidates[0])[: -len(".json")]
    substitutions = {
        "${auth_player_name}": "ProdTest",
        "${version_name}": MC_VERSION,
        "${game_directory}": game,
        "${assets_root}": assets,
        "${assets_index_name}": index_name,
        "${auth_uuid}": str(uuid.UUID(int=0)).replace("-", ""),
        "${auth_access_token}": "0",
        "${user_properties}": "{}",
        "${user_type}": "legacy",
    }
    out = [substitutions.get(a, a) for a in mc_args.split(" ")]
    for tweaker in tweakers:
        out += ["--tweakClass", tweaker]
    return out


def prism_command(args, work, game, libraries, natives):
    zips = glob.glob(os.path.join(ROOT, "build", "distributions", "*-multimc.zip"))
    if len(zips) != 1:
        sys.exit("Run ./gradlew assemble first (expected exactly one multimc zip)")
    instance = os.path.join(work, "instance")
    shutil.rmtree(instance, ignore_errors=True)
    with zipfile.ZipFile(zips[0]) as z:
        z.extractall(instance)
    with open(os.path.join(instance, "mmc-pack.json"), encoding="utf-8") as f:
        pack = json.load(f)
    components = []
    for component in pack["components"]:
        with open(os.path.join(instance, "patches", component["uid"] + ".json"), encoding="utf-8") as f:
            components.append(json.load(f))
    components.sort(key=lambda c: c.get("order", 0))

    classpath, jvm_args, tweakers = [], [], []
    main_class = mc_args = main_jar = asset_index = None
    for component in components:
        jvm_args += component.get("+jvmArgs", [])
        if "FirstThreadOnMacOS" in component.get("+traits", []) and current_os() == "osx":
            # What Prism does for this trait: macOS only lets the JVM's first thread drive windows
            jvm_args.append("-XstartOnFirstThread")
        tweakers += component.get("+tweakers", [])
        main_class = component.get("mainClass", main_class)
        mc_args = component.get("minecraftArguments", mc_args)
        main_jar = component.get("mainJar", main_jar)
        asset_index = component.get("assetIndex", asset_index)
        classpath += resolve_libraries(component.get("libraries", []), libraries, natives,
                                       os.path.join(instance, "libraries"))
    art = main_jar["downloads"]["artifact"]
    classpath.append(download(art["url"], os.path.join(libraries, maven_path(main_jar["name"])), art.get("sha1")))
    cmd = [args.java] + jvm_args + ["-Djava.library.path=" + natives, "-cp", os.pathsep.join(classpath), main_class]
    return cmd + game_arguments(mc_args, game, args.assets, asset_index["id"], tweakers), classpath


def mixinbooter(libraries):
    """MixinBooter 12.2 from Modrinth, checked against its pinned SHA-1."""
    meta = json.loads(fetch("https://api.modrinth.com/v2/project/mixinbooter/version?game_versions=%5B%221.12.2%22%5D"))
    for version in meta:
        for file in version["files"]:
            if file["hashes"].get("sha1") == MIXINBOOTER[1]:
                return download(file["url"], os.path.join(libraries, "mods", file["filename"].lstrip("!")), MIXINBOOTER[1])
    sys.exit("MixinBooter 12.2 is no longer listed on Modrinth; update MIXINBOOTER")


def main():
    ap = argparse.ArgumentParser()
    java_home = os.environ.get("JAVA_HOME")
    ap.add_argument("--java", default=os.path.join(java_home, "bin", "java") if java_home else "java",
                    help="Modern (17+) java executable")
    ap.add_argument("--work", help="Work directory (default build/prodtest)")
    ap.add_argument(
        "--assets",
        default=os.path.join(os.path.expanduser("~"), ".gradle", "caches", "essential-loom", "assets"),
        help="An assets directory containing the 1.12 asset index (a Gradle/loom cache or a .minecraft/assets)")
    ap.add_argument("--run-seconds", type=int, default=30, help="How long to keep the game running after Forge loaded")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    mods = [j for j in glob.glob(os.path.join(ROOT, "build", "libs", "lwjgl3ify-*.jar"))
            if not any(c in j for c in ("forgePatches", "-dev", "-sources", "-api"))]
    if len(mods) != 1:
        sys.exit("Run ./gradlew assemble first (expected exactly one mod jar in build/libs)")

    work = os.path.abspath(args.work or os.path.join(ROOT, "build", "prodtest"))
    libraries = os.path.join(work, "libraries")
    natives = os.path.join(work, "natives")
    game = os.path.join(work, "game")
    for folder in (natives, os.path.join(game, "mods"), os.path.join(game, "crash-reports")):
        shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(natives)
    os.makedirs(os.path.join(game, "mods"))
    for mod in mods + [mixinbooter(libraries)]:
        shutil.copy(mod, os.path.join(game, "mods"))

    cmd, classpath = prism_command(args, work, game, libraries, natives)
    print("Command:", cmd[0], "...", cmd[cmd.index("-cp") + 2], " ".join(cmd[cmd.index("-cp") + 3:]))
    print("Classpath:", *[os.path.relpath(p, work) for p in classpath], sep=os.linesep + "  ")
    log_path = os.path.join(work, "prodtest.log")
    loaded_at = None
    alive = False
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(cmd, cwd=game, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        deadline = time.time() + args.timeout
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout] and lines.put(None), daemon=True).start()
        while time.time() < deadline:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                line = ""
            if line is None or (proc.poll() is not None and lines.empty()):
                break
            if line:
                log.write(line)
                if any(m in line for m in (LOADED, "LWJGL Version", "FATAL", "Exception in thread", "Java is ")):
                    print(line.rstrip())
                if LOADED in line and loaded_at is None:
                    loaded_at = time.time()
            if loaded_at is not None and time.time() - loaded_at >= args.run_seconds:
                alive = proc.poll() is None
                break
        if proc.poll() is None:
            proc.kill()
    crashes = glob.glob(os.path.join(game, "crash-reports", "*"))
    print("Forge loaded:", loaded_at is not None, "| still running after", args.run_seconds, "s:", alive,
          "| crash reports:", crashes or "none", "| log:", log_path)
    passed = loaded_at is not None and alive and not crashes
    print("Production test", "PASSED" if passed else "FAILED")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
