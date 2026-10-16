# Compila ggml-rpc-server e llama-server per il PC (Linux, Windows, macOS) in native/build-pc/bin.
# Stesso sorgente e stesse patch dei telefoni e del Pi: è obbligatorio, altrimenti il protocollo RPC non combacia.
# Uso: python native/build-pc.py [--gpu]   (serve llama.cpp clonato in native/llama.cpp al commit indicato nel README)
#   --gpu: backend GPU in native/build-pc-gpu (Vulkan su Linux e Windows, Metal su macOS); per Vulkan servono
#          l'SDK (variabile VULKAN_SDK, oppure headers, vulkan-1 e glslc trovati da cmake) e il driver della GPU.
# Requisiti: git, cmake e un compilatore C/C++ nel PATH (gcc/clang; su Windows MSVC o llvm-mingw); ninja facoltativo.
# Binari statici con le istruzioni della CPU locale (AVX2/FMA/F16C...): vanno ricompilati su ogni PC.
import glob, os, platform, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
GPU = '--gpu' in sys.argv[1:]
SRC, OUT = os.path.join(HERE, 'llama.cpp'), os.path.join(HERE, 'build-pc-gpu' if GPU else 'build-pc')
OS = platform.system()

if not os.path.isdir(SRC):
    sys.exit(f'manca {SRC}: git clone https://github.com/ggml-org/llama.cpp {SRC} && git -C {SRC} checkout 69f201a')
for p in sorted(glob.glob(os.path.join(HERE, 'patches', '*.patch'))):  # patch DistAI (applicate una volta sola)
    if subprocess.run(['git', '-C', SRC, 'apply', '--reverse', '--check', p], capture_output=True).returncode:
        subprocess.run(['git', '-C', SRC, 'apply', p], check=True)

cfg = ['cmake', '-S', SRC, '-B', OUT, '-DCMAKE_BUILD_TYPE=Release', '-DBUILD_SHARED_LIBS=OFF',
       '-DGGML_RPC=ON', '-DGGML_NATIVE=ON', '-DGGML_OPENMP=OFF', '-DGGML_LLAMAFILE=ON',
       '-DLLAMA_OPENSSL=OFF', '-DLLAMA_BUILD_TESTS=OFF', '-DLLAMA_BUILD_EXAMPLES=OFF', '-DLLAMA_BUILD_UI=OFF',
       '-DLLAMA_USE_PREBUILT_UI=OFF', '-DLLAMA_SUBPROCESS=OFF', f'-DCMAKE_RUNTIME_OUTPUT_DIRECTORY={OUT}/bin']
if shutil.which('ninja'):
    cfg += ['-G', 'Ninja']
if OS == 'Windows' and not shutil.which('cl') and shutil.which('clang'):
    # llvm-mingw: eseguibili statici, senza DLL di libc++ da portarsi dietro
    # _WIN32_WINNT: mingw parte da Windows 7, cpp-httplib usa API di Windows 8+ (CreateFile2)
    cfg += ['-DCMAKE_C_COMPILER=clang', '-DCMAKE_CXX_COMPILER=clang++', '-DCMAKE_EXE_LINKER_FLAGS=-static',
            '-DCMAKE_C_FLAGS=-D_WIN32_WINNT=0x0A00', '-DCMAKE_CXX_FLAGS=-D_WIN32_WINNT=0x0A00']
if OS == 'Darwin':
    cfg += [f'-DGGML_METAL={"ON" if GPU else "OFF"}']
elif GPU:
    cfg += ['-DGGML_VULKAN=ON']

with open(OUT + '.log', 'w') as log:
    for cmd in (cfg, ['cmake', '--build', OUT, '--config', 'Release', '-j', str(os.cpu_count() or 4),
                      '--target', 'ggml-rpc-server', 'llama-server']):
        if subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode:
            sys.exit(f'compilazione fallita, vedi {log.name}')
for exe in ('ggml-rpc-server', 'llama-server'):
    print(os.path.join(OUT, 'bin', exe + ('.exe' if OS == 'Windows' else '')))
