import Foundation

/// Bytes this app is willing to write, and the fields it is willing to name.
///
/// Anything sent is one of the commands published for WHOOP 5/MG:
/// - Wilson's playground `command()` / `FLAGS` / `buildR22()`:
///   https://judes.club/experiments/whoop5/
/// - Wilson's write-up (cursor at inner[13:21], write-with-response, do not send 0x42):
///   https://judes.club/writing/cracking-the-whoop-5-bluetooth-protocol/
/// - NOOP `docs/PROTOCOL_WHOOP5.md` fixed hello
///   `AA 01 08 00 00 01 E6 71 23 01 91 01 36 3E 5C 8D`
/// - NOOP `Whoop5ConfigTests` golden `enable_r22_packets` frame
/// - NOOP `docs/PROTOCOL_TRANSPORT.md` format 1, and history body `01` plus the
///   original 8-byte HISTORY_END token, only after the chunk is stored
/// - NOOP `docs/PROTOCOL_SENSORS.md` packet 40, R18, R21, R26
///
/// Buzz, alarm, reboot, DFU and trim are refused here. There is no other opcode.
enum WhoopCodec {
    static let flags: [(name: String, value: UInt8)] = [
        ("enable_r22_packets", 0x32),
        ("enable_r22_v2_packets", 0x32),
        ("enable_r22_v3_packets", 0x32),
        ("enable_r22_v4_packets", 0x31),
        ("enable_r22_v5_packets", 0x32),
        ("enable_r22_v6_packets", 0x32),
        ("enable_r22_v8_packets", 0x32),
        ("make_hrfm_visible", 0x32),
        ("disable_pip_r26_packets", 0x32),
        ("wear_detect_bias", 0x32),
        ("hr_ch_switching", 0x32),
        ("ir_hw_switching", 0x32),
        ("enable_passive_strap_fit_gen5", 0x31),
        ("enable_sig11_during_sleep", 0x32),
        ("dorset_inhibit_wpt", 0x32),
    ]

    /// NOOP CommandNumber entries that buzz, alarm, reboot, load firmware, or move the trim cursor.
    static let forbidden: Set<UInt8> = [
        0x13, 0x19, 0x1D, 0x20, 0x21, 0x24, 0x25, 0x26, 0x2D,
        0x42, 0x44, 0x45, 0x4F, 0x53, 0x8E, 0x8F, 0x90,
    ]

    static let allowed: Set<UInt8> = [0x91, 0x8D, 0x78, 0x22, 0x16, 0x17]

    static func crc16Modbus(_ bytes: ArraySlice<UInt8>) -> UInt16 {
        var crc: UInt16 = 0xFFFF
        for b in bytes {
            crc ^= UInt16(b)
            for _ in 0..<8 {
                crc = (crc & 1) != 0 ? (crc >> 1) ^ 0xA001 : crc >> 1
            }
        }
        return crc
    }

    static func crc32(_ bytes: ArraySlice<UInt8>) -> UInt32 {
        var crc: UInt32 = 0xFFFFFFFF
        for b in bytes {
            crc ^= UInt32(b)
            for _ in 0..<8 {
                crc = (crc & 1) != 0 ? (crc >> 1) ^ 0xEDB88320 : crc >> 1
            }
        }
        return crc ^ 0xFFFFFFFF
    }

    /// Wilson `encodeFrame`: `AA 01`, length, field `00 01`, CRC-16/MODBUS, inner, zlib CRC-32.
    static func encodeFrame(inner: [UInt8]) -> [UInt8] {
        let declared = 2 + 2 + inner.count
        var frame: [UInt8] = [
            0xAA, 0x01,
            UInt8(declared & 0xFF), UInt8((declared >> 8) & 0xFF),
            0x00, 0x01,
        ]
        let c16 = crc16Modbus(frame[0..<6])
        frame.append(UInt8(c16 & 0xFF))
        frame.append(UInt8((c16 >> 8) & 0xFF))
        frame.append(contentsOf: inner)
        let c32 = crc32(inner[0..<inner.count])
        frame.append(UInt8(c32 & 0xFF))
        frame.append(UInt8((c32 >> 8) & 0xFF))
        frame.append(UInt8((c32 >> 16) & 0xFF))
        frame.append(UInt8((c32 >> 24) & 0xFF))
        return frame
    }

    /// Returns nil for an opcode this app must not send, or one nobody published for this session.
    static func command(cmd: UInt8, seq: UInt8, b3: UInt8, payload: [UInt8] = []) -> [UInt8]? {
        if forbidden.contains(cmd) || !allowed.contains(cmd) { return nil }
        var inner: [UInt8] = [0x23, seq, cmd, b3]
        inner.append(contentsOf: payload)
        let pad = (4 - inner.count % 4) % 4
        if pad > 0 { inner.append(contentsOf: [UInt8](repeating: 0, count: pad)) }
        return encodeFrame(inner: inner)
    }

    /// Wilson `setConfigPayload`: name, NUL pad to 32, value byte, seven zeros.
    static func setConfigPayload(name: String, value: UInt8) -> [UInt8] {
        var payload = [UInt8](repeating: 0, count: 40)
        let bytes = Array(name.utf8.prefix(32))
        for (i, b) in bytes.enumerated() { payload[i] = b }
        payload[32] = value
        return payload
    }

    static func verify(_ frame: [UInt8]) -> Bool {
        guard frame.count >= 16, frame[0] == 0xAA, frame[1] == 0x01 else { return false }
        let declared = Int(frame[2]) | (Int(frame[3]) << 8)
        guard declared + 8 == frame.count else { return false }
        guard crc16Modbus(frame[0..<6]) == u16(frame, 6) else { return false }
        return crc32(frame[8..<(frame.count - 4)]) == u32(frame, frame.count - 4)
    }

    static func u16(_ frame: [UInt8], _ off: Int) -> UInt16 {
        UInt16(frame[off]) | (UInt16(frame[off + 1]) << 8)
    }

    static func u32(_ frame: [UInt8], _ off: Int) -> UInt32 {
        UInt32(frame[off])
            | (UInt32(frame[off + 1]) << 8)
            | (UInt32(frame[off + 2]) << 16)
            | (UInt32(frame[off + 3]) << 24)
    }

    static func i16(_ frame: [UInt8], _ off: Int) -> Int {
        Int(Int16(bitPattern: u16(frame, off)))
    }

    static func f32(_ frame: [UInt8], _ off: Int) -> Double {
        let bits = u32(frame, off)
        return Double(Float(bitPattern: bits))
    }

    /// NOOP sensors: integer milliseconds are `(ticks * 1000 + 512) / 1024`.
    static func rrMilliseconds(ticks: Int) -> Int {
        (ticks * 1000 + 512) / 1024
    }

    struct HeartRate {
        var bpm: Int
        var rrMs: [Int]
        var contact: Bool?
    }

    /// Bluetooth Heart Rate Measurement (0x2A37). Wilson: flags, then HR, then R-R when the flag says so.
    static func decodeHeartRate(_ data: [UInt8]) -> HeartRate? {
        guard let flags = data.first else { return nil }
        var i = 1
        let bpm: Int
        if flags & 0x01 != 0 {
            guard data.count >= i + 2 else { return nil }
            bpm = Int(data[i]) | (Int(data[i + 1]) << 8)
            i += 2
        } else {
            guard data.count > i else { return nil }
            bpm = Int(data[i])
            i += 1
        }
        if flags & 0x08 != 0 {
            guard data.count >= i + 2 else { return nil }
            i += 2
        }
        var rr: [Int] = []
        if flags & 0x10 != 0 {
            while i + 1 < data.count {
                let ticks = Int(data[i]) | (Int(data[i + 1]) << 8)
                i += 2
                if ticks != 0 { rr.append(rrMilliseconds(ticks: ticks)) }
            }
        }
        let contact: Bool? = (flags & 0x04) != 0 ? (flags & 0x02) != 0 : nil
        return HeartRate(bpm: bpm, rrMs: rr, contact: contact)
    }

    struct Frame {
        var kind: String
        var fields: [String: Any]
        var cursor: [UInt8]?
    }

    /// Name a measurement only when a published 5/MG layout maps that byte. Otherwise `raw`.
    static func decodePrivate(_ frame: [UInt8]) -> Frame {
        guard verify(frame) else { return Frame(kind: "raw", fields: ["reason": "crc"], cursor: nil) }
        let ptype = frame[8]
        let layout = frame.count > 9 ? frame[9] : 0
        let end = frame.count - 4
        var fields: [String: Any] = ["packetType": Int(ptype)]

        if ptype == 36, end >= 13 {
            fields["cmd"] = Int(frame[10])
            fields["origin"] = Int(frame[11])
            fields["result"] = Int(frame[12])
            return Frame(kind: "command-response", fields: fields, cursor: nil)
        }

        // Wilson: type 0x31 subtype 0x02 carries the offload cursor at inner[13:21].
        // NOOP: metadata 2 is HISTORY_END. The eight bytes are echoed, not rebuilt.
        if ptype == 0x31, frame.count >= 33, frame[10] == 0x02, 29 <= end {
            let cursor = Array(frame[21..<29])
            fields["cursor"] = Hex.encode(cursor)
            return Frame(kind: "history-end", fields: fields, cursor: cursor)
        }
        if ptype == 0x31, frame.count > 10, frame[10] == 0x01 {
            return Frame(kind: "history-start", fields: fields, cursor: nil)
        }
        if ptype == 0x31, frame.count > 10, frame[10] == 0x03 {
            return Frame(kind: "history-complete", fields: fields, cursor: nil)
        }

        if ptype == 40, end >= 18 {
            let n = Int(frame[17])
            var rr: [Int] = []
            for i in 0..<n {
                let off = 18 + 2 * i
                if off + 2 > end { break }
                let ticks = Int(u16(frame, off))
                if ticks != 0 { rr.append(rrMilliseconds(ticks: ticks)) }
            }
            fields["unix"] = Int(u32(frame, 10))
            fields["hr"] = Int(frame[16])
            fields["rrMs"] = rr
            return Frame(kind: "packet40", fields: fields, cursor: nil)
        }

        if ptype == 0x2F, frame.count == 124, layout == 18 {
            fields["unix"] = Int(u32(frame, 15))
            fields["hr"] = Int(frame[22])
            fields["rrCount"] = Int(frame[23])
            fields["hr2"] = Int(frame[37])
            fields["hrFlagsRaw"] = Int(frame[36])
            fields["activityRaw"] = Int(frame[63])
            var rr: [Int] = []
            for i in 0..<min(Int(frame[23]), 4) {
                let off = 24 + 2 * i
                if off + 2 > end { break }
                let ticks = Int(u16(frame, off))
                if ticks != 0 { rr.append(rrMilliseconds(ticks: ticks)) }
            }
            fields["rrMs"] = rr
            let dyn = f32(frame, 41)
            if dyn.isFinite, (0...8).contains(dyn) { fields["dynamicAccelG"] = dyn }
            let grav = [f32(frame, 45), f32(frame, 49), f32(frame, 53)]
            if grav.allSatisfy(\.isFinite) { fields["gravityG"] = grav }
            let skinRaw = Int(u16(frame, 73))
            fields["skinTempRaw"] = skinRaw
            let skinC = Double(skinRaw) / 100
            if (5...45).contains(skinC) { fields["skinTempC"] = skinC }
            for (name, off) in [("aux0", 69), ("aux1", 71)] {
                let raw = i16(frame, off)
                fields[name + "Raw"] = raw
                let celsius = Double(raw) / 10
                if (0...60).contains(celsius) { fields[name + "C"] = celsius }
            }
            if let label = ["still", "walk", "run"][safe: Int(frame[63])] {
                fields["activity"] = label
            }
            return Frame(kind: "r18", fields: fields, cursor: nil)
        }

        let innerLen = frame.count - 12
        if ptype == 0x2F, innerLen == 112, (frame[10] == 0x80 || frame[10] == 0x82), layout != 18 {
            fields["unix"] = Int(u32(frame, 15))
            fields["hr"] = Int(frame[22])
            fields["hr2"] = Int(frame[37])
            fields["cmd"] = Int(frame[10])
            let accel = [f32(frame, 45), f32(frame, 49), f32(frame, 53)]
            if accel.allSatisfy(\.isFinite) { fields["accelG"] = accel }
            return Frame(kind: "r22", fields: fields, cursor: nil)
        }

        if (ptype == 43 || ptype == 47), layout == 21, frame.count == 1244 {
            let ac = min(Int(u16(frame, 24)), 100)
            let gc = min(Int(u16(frame, 630)), 100)
            var accel: [[Int]] = []
            for i in 0..<ac {
                let x = 28 + 2 * i, y = 228 + 2 * i, z = 428 + 2 * i
                if z + 2 > end { break }
                accel.append([i16(frame, x), i16(frame, y), i16(frame, z)])
            }
            var gyro: [[Int]] = []
            for i in 0..<gc {
                let x = 640 + 2 * i, y = 840 + 2 * i, z = 1040 + 2 * i
                if z + 2 > end { break }
                gyro.append([i16(frame, x), i16(frame, y), i16(frame, z)])
            }
            fields["unix"] = Int(u32(frame, 15))
            fields["accelCount"] = accel.count
            fields["gyroCount"] = gyro.count
            fields["accelRaw"] = accel
            fields["gyroRaw"] = gyro
            return Frame(kind: "r21", fields: fields, cursor: nil)
        }

        if ptype == 47, layout == 26, frame.count == 88 {
            fields["unix"] = Int(u32(frame, 15))
            let motion = f32(frame, 75)
            if motion.isFinite { fields["summaryMotionRaw"] = motion }
            return Frame(kind: "r26", fields: fields, cursor: nil)
        }

        if ptype == 47, layout == 20, frame.count == 2140 {
            // Optical blocks. No published HR, R-R, motion or skin temperature on this layout.
            fields["layout"] = 20
            return Frame(kind: "r20", fields: fields, cursor: nil)
        }

        if ptype == 48, end >= 16 {
            fields["event"] = Int(frame[10])
            fields["unix"] = Int(u32(frame, 12))
            return Frame(kind: "event", fields: fields, cursor: nil)
        }

        return Frame(kind: "raw", fields: fields, cursor: nil)
    }

    /// Nil when the codec still matches the published vectors in `selfcheck.json`.
    static func selfCheck() -> String? {
        guard let url = Bundle.main.url(forResource: "selfcheck", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return "Falta selfcheck.json en el bundle." }

        guard let hello = command(cmd: 0x91, seq: 1, b3: 0x01),
              Hex.encode(hello) == json["hello"] as? String
        else { return "GET_HELLO no coincide con el saludo publicado por NOOP." }

        let payload = setConfigPayload(name: "enable_r22_packets", value: 0x32)
        guard let flag = command(cmd: 0x78, seq: 1, b3: 0x01, payload: payload),
              Hex.encode(flag) == json["enableR22Seq1"] as? String
        else { return "SET_CONFIG enable_r22_packets no coincide con el frame dorado de NOOP." }

        if let published = json["flags"] as? [[String: Any]] {
            guard published.count == flags.count else { return "La lista FLAGS no tiene 15 entradas." }
            for (item, flag) in zip(published, flags) {
                let value = (item["value"] as? Int) ?? (item["value"] as? NSNumber)?.intValue
                guard item["name"] as? String == flag.name, value == Int(flag.value) else {
                    return "FLAGS no coincide con el playground de Wilson."
                }
            }
        }

        if command(cmd: 0x42, seq: 1, b3: 0x04) != nil { return "El codec construyó SET_ALARM_TIME." }
        for cmd: UInt8 in [0x19, 0x1D, 0x20, 0x21, 0x2D] {
            if command(cmd: cmd, seq: 1, b3: 0x00) != nil {
                return String(format: "El codec construyó un comando prohibido 0x%02X.", cmd)
            }
        }

        guard let ackSpec = json["ack"] as? [String: Any],
              let cursorHex = ackSpec["cursor"] as? String,
              let ackFrame = ackSpec["frame"] as? String,
              let seq = jsonInt(ackSpec["seq"]),
              let built = command(cmd: 0x17, seq: UInt8(seq), b3: 0x01, payload: Hex.decode(cursorHex)),
              Hex.encode(built) == ackFrame
        else { return "El ack 0x17 no coincide con el cursor publicado." }

        guard let split = json["helloSplit"] as? [String] else { return "Falta la prueba de reensamblado." }
        var assembler = FrameReassembler()
        var frames: [[UInt8]] = []
        for part in split { frames.append(contentsOf: assembler.feed(Hex.decode(part))) }
        guard frames.count == 1, Hex.encode(frames[0]) == json["hello"] as? String else {
            return "El reensamblado no recuperó el saludo."
        }

        guard let cases = json["cases"] as? [[String: Any]] else { return "selfcheck.json no tiene casos." }
        for item in cases {
            if let hrBytes = item["hrBytes"] as? String {
                guard let hr = decodeHeartRate(Hex.decode(hrBytes)),
                      hr.bpm == jsonInt(item["hr"]),
                      hr.rrMs == jsonInts(item["rrMs"])
                else { return "2A37 no decodifica el caso publicado." }
                continue
            }
            if let battery = item["batteryByte"] as? String {
                let byte = Hex.decode(battery)
                guard byte.count == 1, jsonInt(item["battery"]) == Int(byte[0]) else {
                    return "2A19 no decodifica el caso publicado."
                }
                continue
            }
            guard let frameHex = item["frame"] as? String, let kind = item["kind"] as? String else {
                return "Caso de selfcheck incompleto."
            }
            let decoded = decodePrivate(Hex.decode(frameHex))
            if decoded.kind != kind { return "Se esperaba \(kind) y salió \(decoded.kind)." }
            for key in ["hr", "hr2", "unix", "rrMs", "skinTempC", "skinTempRaw", "gravityG", "dynamicAccelG", "activity", "accelG", "event", "accelRaw", "gyroRaw", "summaryMotionRaw"] {
                guard let expected = item[key] else { continue }
                guard let got = decoded.fields[key], jsonSame(got, expected) else {
                    return "\(kind).\(key) no coincide con el vector publicado."
                }
            }
            if let forbid = item["forbid"] as? [String] {
                for key in forbid where decoded.fields[key] != nil {
                    return "\(kind) inventó el campo \(key)."
                }
            }
            if let cursor = item["cursor"] as? String {
                guard let got = decoded.cursor, Hex.encode(got) == cursor else {
                    return "El cursor de HISTORY_END no está en inner[13:21]."
                }
            }
        }
        return nil
    }
}

enum Hex {
    static func encode(_ bytes: [UInt8]) -> String {
        bytes.map { String(format: "%02X", $0) }.joined()
    }

    static func decode(_ text: String) -> [UInt8] {
        var out: [UInt8] = []
        var index = text.startIndex
        while index < text.endIndex {
            let next = text.index(index, offsetBy: 2, limitedBy: text.endIndex) ?? text.endIndex
            if let byte = UInt8(text[index..<next], radix: 16) { out.append(byte) }
            index = next
        }
        return out
    }

    static func preview(_ bytes: [UInt8], limit: Int = 24) -> String {
        let head = bytes.prefix(limit).map { String(format: "%02X", $0) }.joined(separator: " ")
        return bytes.count > limit ? head + " …" : head
    }
}

final class FrameReassembler {
    private var buf: [UInt8] = []

    func feed(_ fragment: [UInt8]) -> [[UInt8]] {
        buf.append(contentsOf: fragment)
        var out: [[UInt8]] = []
        while true {
            guard let sof = buf.firstIndex(of: 0xAA) else {
                buf.removeAll(keepingCapacity: true)
                break
            }
            if sof > 0 { buf.removeFirst(sof) }
            if buf.count < 8 { break }
            let declared = Int(buf[2]) | (Int(buf[3]) << 8)
            let total = declared + 8
            let headerOK = buf[1] == 0x01 && WhoopCodec.crc16Modbus(buf[0..<6]) == WhoopCodec.u16(buf, 6)
            if total < 16 || total > 8192 || !headerOK {
                buf.removeFirst(1)
                continue
            }
            if buf.count < total { break }
            let frame = Array(buf[0..<total])
            buf.removeFirst(total)
            if WhoopCodec.verify(frame) { out.append(frame) }
        }
        return out
    }
}

private func jsonInt(_ value: Any?) -> Int? {
    if let value = value as? Int { return value }
    if let value = value as? NSNumber { return value.intValue }
    return nil
}

private func jsonInts(_ value: Any?) -> [Int]? {
    if let value = value as? [Int] { return value }
    if let value = value as? [NSNumber] { return value.map(\.intValue) }
    if let value = value as? [Any] { return value.compactMap { jsonInt($0) } }
    return nil
}

private func jsonSame(_ got: Any, _ expected: Any) -> Bool {
    canon(got) == canon(expected)
}

private func canon(_ value: Any) -> String {
    if let text = value as? String { return "s:" + text }
    if let rows = value as? [[Int]] {
        return "a:[" + rows.map { canon($0) }.joined(separator: ",") + "]"
    }
    if let numbers = value as? [Int] {
        return "a:[" + numbers.map { "n:\($0)" }.joined(separator: ",") + "]"
    }
    if let numbers = value as? [Double] {
        return "a:[" + numbers.map { num($0) }.joined(separator: ",") + "]"
    }
    if let list = value as? [Any] {
        return "a:[" + list.map { canon($0) }.joined(separator: ",") + "]"
    }
    if let whole = value as? Int { return "n:\(whole)" }
    if let real = value as? Double { return num(real) }
    if let number = value as? NSNumber { return num(number.doubleValue) }
    return "?"
}

private func num(_ value: Double) -> String {
    guard value.isFinite else { return "nan" }
    if value.rounded() == value, abs(value) < 1_000_000_000_000 {
        return "n:\(Int(value))"
    }
    return String(format: "d:%.5f", value)
}

private extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
