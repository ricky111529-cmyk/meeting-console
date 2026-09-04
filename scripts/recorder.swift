// 미팅 녹음기 — AVAudioRecorder 기반
//
// ffmpeg의 avfoundation 직접 캡처를 대체한다 (2026-08-19).
// 왜 바꿨나: ffmpeg 캡처는 오디오가 깨져 나왔다. 길이는 맞는데 말이 빨리감기처럼
//   들리고 잡음이 심해 알아들을 수 없었다 (같은 환경에서 17kbps 대 64kbps).
//   자동 녹음 4건 전부 STT 불가.
//   음성 메모·Zoom(=정식 CoreAudio 세션)으로 받은 4건은 전부 정상이었다.
//   AVAudioRecorder는 음성 메모가 쓰는 것과 같은 API라 협상을 OS가 처리한다.
//
// 사용법: recorder <출력경로.m4a> [녹음초]
//   녹음초 생략 시 SIGINT(Ctrl+C)까지 계속 녹음한다.

import Foundation
import AVFoundation

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("사용법: recorder <출력경로.m4a> [녹음초]\n".data(using: .utf8)!)
    exit(2)
}
let url = URL(fileURLWithPath: args[1])
let duration: Double? = args.count >= 3 ? Double(args[2]) : nil

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write("❌ \(msg)\n".data(using: .utf8)!)
    exit(1)
}

// 마이크 권한이 이미 거부돼 있으면 빈 파일을 남기지 않고 즉시 알린다
switch AVCaptureDevice.authorizationStatus(for: .audio) {
case .denied, .restricted:
    fail("마이크 권한이 거부돼 있습니다. 시스템 설정 > 개인정보 보호 및 보안 > 마이크")
default: break
}

// 44.1kHz mono AAC — 음성 메모가 만드는 것과 같은 규격.
// 16kHz 변환은 녹음이 끝난 뒤 transcribe 단계에서 한다 (캡처 중에는 아무것도 건드리지 않는다).
let settings: [String: Any] = [
    AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
    AVSampleRateKey: 44100.0,
    AVNumberOfChannelsKey: 1,
    AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
    AVEncoderBitRateKey: 64000,
]

final class Delegate: NSObject, AVAudioRecorderDelegate {
    func audioRecorderDidFinishRecording(_ r: AVAudioRecorder, successfully ok: Bool) {
        print(ok ? "stopped" : "finished-with-error")
        exit(ok ? 0 : 1)
    }
    func audioRecorderEncodeErrorDidOccur(_ r: AVAudioRecorder, error: Error?) {
        fail("인코딩 오류: \(error?.localizedDescription ?? "알 수 없음")")
    }
}

let delegate = Delegate()
guard let recorder = try? AVAudioRecorder(url: url, settings: settings) else {
    fail("레코더를 만들 수 없습니다: \(url.path)")
}
recorder.delegate = delegate
guard recorder.prepareToRecord() else { fail("녹음 준비 실패") }

// SIGINT/SIGTERM을 받으면 stop()으로 파일을 정상 마감한다.
//
// ⚠️ DispatchSource 객체는 반드시 전역으로 붙잡아 둬야 한다 (2026-08-20 수정).
//    지역 변수로 만들면 resume() 직후 해제되어 시그널이 전달되지 않는다.
//    이 버그로 SIGINT·SIGTERM이 모두 무시되어, 59분 녹음을 강제 종료할 수밖에 없었고
//    moov(색인)가 안 쓰여 28MB 오디오를 재생할 수 없게 됐다.
var signalSources: [DispatchSourceSignal] = []
func installStopHandler(_ sig: Int32) {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { recorder.stop() }   // stop() → 델리게이트 → 파일 마감 후 exit
    src.resume()
    signalSources.append(src)                 // 해제 방지
}
installStopHandler(SIGINT)
installStopHandler(SIGTERM)

// 벽시계 기준 마감을 따로 건다.
//  record(forDuration:)은 "실제 녹음된 시간"을 세기 때문에, 중간에 끊긴 구간이 있으면
//  벽시계로는 지정 시간을 넘겨서까지 녹음한다 (2026-08-20: 3630초 지정, 벽시계 61분 시점에
//  녹음된 분량은 59분 14초 → 타이머 만료까지 76초가 더 남아 있었다).
//  타이머 고장이 아니라 정상 동작이지만, 무인 실행에서는 벽시계 상한도 필요하므로 이중으로 건다.
var deadlineTimer: DispatchSourceTimer?
if let d = duration {
    let t = DispatchSource.makeTimerSource(queue: .main)
    t.schedule(deadline: .now() + d)
    t.setEventHandler { recorder.stop() }
    t.resume()
    deadlineTimer = t
}
_ = deadlineTimer

// record()의 반환값을 반드시 확인한다 (2026-08-19 수정).
//  확인하지 않았을 때: 시작 실패 시 28바이트 헤더만 남고, 델리게이트 콜백이 오지 않아
//  RunLoop이 영원히 돌았다. 실제로 1시간 56분을 그렇게 방치했다.
//  시작 실패는 일시적일 수 있으므로 몇 번 재시도한다.
var started = false
for attempt in 1...5 {
    started = duration.map { recorder.record(forDuration: $0) } ?? recorder.record()
    if started { break }
    let msg = "녹음 시작 실패 (\(attempt)/5), 2초 후 재시도\n"
    FileHandle.standardError.write(msg.data(using: .utf8)!)
    Thread.sleep(forTimeInterval: 2)
}
guard started else {
    try? FileManager.default.removeItem(at: url)   // 헤더만 있는 파일을 남기지 않는다
    fail("녹음을 시작할 수 없습니다 (5회 시도). 마이크를 다른 앱이 독점하고 있거나 장치 상태가 비정상입니다.")
}

// 시작 직후 실제로 녹음 중인지 확인한다 (record()가 true를 반환하고도 멈춘 경우 대비)
Thread.sleep(forTimeInterval: 1)
guard recorder.isRecording else {
    try? FileManager.default.removeItem(at: url)
    fail("녹음이 시작됐다가 즉시 멈췄습니다.")
}

print("recording")
RunLoop.main.run()
