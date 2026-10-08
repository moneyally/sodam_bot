"""ntgcalls v3.0.0 소스에 '음질 설정을 환경변수로' 패치 (오디오 실험실, docs/AUDIO_LAB.md).

아무 환경변수도 없으면 원래와 똑같이 동작 (32k 모노 통화 모드, ptime 60) → 그냥 깔아도 안전.
  NTG_OPUS_BITRATE   예: 128000  → Opus maxaveragebitrate + RTP 인코딩 max_bitrate_bps
  NTG_OPUS_MIN_BITRATE 예: 96000 → RTP 인코딩 min_bitrate_bps (대역폭 추정이 너무 깎지 않게, 선택)
  NTG_OPUS_STEREO    1           → stereo=1·sprop-stereo=1 (WebRTC 가 음악 모드 kAudio 로 바뀌는 유일한 길)
  NTG_OPUS_PTIME     20          → 패킷 길이 ms (원래 60)
사용: python patch_hq.py <ntgcalls 소스 폴더>   (줄이 안 맞으면 바로 실패 — 버전 바뀌면 다시 확인)
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
f = root / "wrtc/src/interfaces/media/channels/outgoing_audio_channel.cpp"
s = f.read_text()

def swap(old, new):
    global s
    assert s.count(old) == 1, f"패치 자리를 못 찾음: {old!r}"
    s = s.replace(old, new)

swap("#include <wrtc/interfaces/native_connection.hpp>\n",
     "#include <wrtc/interfaces/native_connection.hpp>\n#include <cstdlib>\n\n"
     "namespace {\n"
     "    // sodam: 음질 실험용 환경변수 (없으면 0 = 원래 동작)\n"
     "    int ntg_env_int(const char* key, const int fallback) {\n"
     "        const char* v = std::getenv(key);\n"
     "        if (v == nullptr || *v == '\\0') return fallback;\n"
     "        const int n = std::atoi(v);\n"
     "        return n > 0 ? n : fallback;\n"
     "    }\n"
     "}\n")
swap("                codec.SetParam(webrtc::kCodecParamPTime, 60);\n",
     "                codec.SetParam(webrtc::kCodecParamPTime, ntg_env_int(\"NTG_OPUS_PTIME\", 60));\n"
     "                if (const int br = ntg_env_int(\"NTG_OPUS_BITRATE\", 0); br > 0) {\n"
     "                    codec.SetParam(\"maxaveragebitrate\", br);\n"
     "                    codec.SetParam(\"usedtx\", 0);\n"
     "                }\n"
     "                if (ntg_env_int(\"NTG_OPUS_STEREO\", 0) == 1) {\n"
     "                    codec.SetParam(\"stereo\", 1);\n"
     "                    codec.SetParam(\"sprop-stereo\", 1);\n"
     "                }\n")
swap("            if (updated_parameters.encodings.empty()) {\n                updated_parameters.encodings.emplace_back();\n            }\n",
     "            if (updated_parameters.encodings.empty()) {\n                updated_parameters.encodings.emplace_back();\n            }\n"
     "            if (const int br = ntg_env_int(\"NTG_OPUS_BITRATE\", 0); br > 0) {\n"
     "                updated_parameters.encodings[0].max_bitrate_bps = br;\n"
     "            }\n"
     "            if (const int mn = ntg_env_int(\"NTG_OPUS_MIN_BITRATE\", 0); mn > 0) {\n"
     "                updated_parameters.encodings[0].min_bitrate_bps = mn;\n"
     "            }\n")
f.write_text(s)
print("패치 완료:", f)
