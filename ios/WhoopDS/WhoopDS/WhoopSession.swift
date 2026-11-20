import Combine
import CoreBluetooth
import Foundation
import UIKit

struct WhoopSnapshot {
    var status = "Preparando Bluetooth"
    var detail = ""
    var selfCheck: String?
    var canSync = false
    var syncing = false
    var connected = false
    var heartRate: Int?
    var heartSource = ""
    var heartRate2: Int?
    var rrText = "—"
    var rrSource = ""
    var contact = ""
    var battery: Int?
    var motionText = "—"
    var motionSource = ""
    var skinText = "—"
    var skinSource = ""
    var model = ""
    var serial = ""
    var serialNote = ""
    var firmware = ""
    var hardware = ""
    var helloFirmware = ""
    var saved = 0
    var ackedChunks = 0
    var fileName = "whoop-history.jsonl"
    var log: [String] = []
}

final class WhoopSession: NSObject, ObservableObject {
    @Published private(set) var snap = WhoopSnapshot()

    private let ble = DispatchQueue(label: "whoop.ble")
    private var central: CBCentralManager!
    private var peripheral: CBPeripheral?
    private var commandChar: CBCharacteristic?
    private var assemblers: [CBUUID: FrameReassembler] = [:]
    private var model = WhoopSnapshot()
    private var publishScheduled = false

    private let serviceID = CBUUID(string: "FD4B0001-CCE1-4033-93CE-002D5875F58A")
    private let heartService = CBUUID(string: "180D")
    private let batteryService = CBUUID(string: "180F")
    private let infoService = CBUUID(string: "180A")
    private let heartRateID = CBUUID(string: "2A37")
    private let batteryID = CBUUID(string: "2A19")
    private let modelID = CBUUID(string: "2A24")
    private let serialID = CBUUID(string: "2A25")
    private let firmwareID = CBUUID(string: "2A26")
    private let hardwareID = CBUUID(string: "2A27")

    private var notifyOK = Set<CBUUID>()
    private var authBlocked = false
    private var outbound: [(kind: Outbound, data: Data)] = []
    private var inflight: Outbound?
    private var stopped = true
    private var userClosed = false
    private var lastRawLog = Date.distantPast
    private var phase = Phase.idle
    private var seq: UInt8 = 1
    private var rangeSeq: UInt8 = 0
    private var store: HistoryStore?
    private var chunkFailed = false
    private var rangeTimeout: DispatchWorkItem?
    private var stallTimeout: DispatchWorkItem?
    private var latestHeart: (bpm: Int, source: String, at: Date)?
    private var latestRR: (text: String, source: String, at: Date)?
    private var latestMotion: (text: String, source: String, at: Date)?
    private var loggedKinds = Set<String>()

    private enum Phase { case idle, handshake, waitRange, offload, finished }
    private enum Outbound { case hello, name, flag(Int), range, historical, ack }

    private var privateNotifies: [CBUUID] { [fd4b(3), fd4b(4), fd4b(5), fd4b(7)] }

    override init() {
        super.init()
        model.selfCheck = WhoopCodec.selfCheck()
        if model.selfCheck != nil {
            model.status = "El codec no coincide con los bytes publicados"
        }
        snap = model
        central = CBCentralManager(delegate: self, queue: ble)
    }

    func startHistory() {
        ble.async { self.beginHistory() }
    }

    func disconnect() {
        ble.async {
            self.userClosed = true
            self.stopped = true
            self.phase = .finished
            self.outbound.removeAll()
            self.model.syncing = false
            self.setIdle(false)
            if let peripheral = self.peripheral {
                self.central.cancelPeripheralConnection(peripheral)
            }
            self.model.status = "Enlace cerrado. Whoop puede volver a conectarse."
            self.model.detail = "Lo ya confirmado salió de la pulsera. Lo que no se confirmó sigue ahí."
            self.refreshReady()
        }
    }

    func searchAgain() {
        ble.async {
            self.userClosed = false
            guard self.model.selfCheck == nil, self.central.state == .poweredOn else { return }
            self.scan()
        }
    }

    private func fd4b(_ suffix: Int) -> CBUUID {
        CBUUID(string: String(format: "FD4B%04X-CCE1-4033-93CE-002D5875F58A", suffix))
    }

    private func beginHistory() {
        guard model.canSync, !model.syncing, model.selfCheck == nil, commandChar != nil else { return }
        do {
            let archive = try HistoryStore()
            store = archive
            try archive.append([
                "kind": "sync-start",
                "receivedAt": timestamp(),
                "oneHost": "La app oficial de Whoop no puede estar conectada. Confirmar un trozo deja que la pulsera suelte ese intervalo.",
                "sources": [
                    "https://judes.club/experiments/whoop5/",
                    "https://judes.club/writing/cracking-the-whoop-5-bluetooth-protocol/",
                    "https://github.com/ryanbr/noop/blob/main/docs/PROTOCOL_WHOOP5.md",
                    "https://github.com/ryanbr/noop/blob/main/docs/PROTOCOL_TRANSPORT.md",
                    "https://github.com/ryanbr/noop/blob/main/docs/PROTOCOL_SENSORS.md",
                ],
            ])
        } catch {
            model.status = "No pude abrir whoop-history.jsonl"
            publish(force: true)
            return
        }
        stopped = false
        chunkFailed = false
        loggedKinds.removeAll()
        model.saved = 0
        model.ackedChunks = 0
        model.syncing = true
        model.canSync = false
        phase = .handshake
        seq = 1
        model.status = "Descargando historial"
        model.detail = "Whoop tiene que seguir cerrado. Al confirmar un trozo, la pulsera lo suelta."
        setIdle(true)
        log("Secuencia publicada: HELLO, nombre, 15 SET_CONFIG, GET_DATA_RANGE, SEND_HISTORICAL.", force: true)
        publish(force: true)
        send(.hello, command(0x91, b3: 0x01))
    }

    private func command(_ cmd: UInt8, b3: UInt8, payload: [UInt8] = []) -> [UInt8]? {
        let current = seq
        guard let frame = WhoopCodec.command(cmd: cmd, seq: current, b3: b3, payload: payload) else { return nil }
        seq = seq &+ 1
        return frame
    }

    private func send(_ kind: Outbound, _ bytes: [UInt8]?) {
        guard !stopped, let bytes else {
            fail("Me negué a construir un comando que no está publicado.")
            return
        }
        outbound.append((kind, Data(bytes)))
        pump()
    }

    private func pump() {
        guard inflight == nil, let characteristic = commandChar, let peripheral else { return }
        guard !outbound.isEmpty else { return }
        let next = outbound.removeFirst()
        guard !stopped || next.kind == .ack else {
            outbound.removeAll()
            return
        }
        inflight = next.kind
        peripheral.writeValue(next.data, for: characteristic, type: .withResponse)
    }

    private func advance(after kind: Outbound) {
        guard !stopped else { return }
        switch kind {
        case .hello:
            log("GET_HELLO 0x91 b3 0x01 · saludo publicado", force: true)
            send(.name, command(0x8D, b3: 0x01))
        case .name:
            log("GET_ADVERTISING_NAME 0x8D b3 0x01", force: true)
            sendFlag(0)
        case .flag(let index):
            if index + 1 < WhoopCodec.flags.count {
                sendFlag(index + 1)
            } else {
                guard let frame = command(0x22, b3: 0x00) else { return }
                rangeSeq = frame[9]
                phase = .waitRange
                log("GET_DATA_RANGE 0x22 b3 0x00", force: true)
                send(.range, frame)
                armRangeTimeout()
            }
        case .range:
            break
        case .historical:
            log("SEND_HISTORICAL 0x16 b3 0x00. El ack 0x17 solo sale después de guardar el trozo.", force: true)
            armStall()
        case .ack:
            model.ackedChunks += 1
            log("Confirmé un trozo con 0x17. La pulsera puede soltar ese tramo.", force: true)
            publish(force: true)
        }
    }

    private func sendFlag(_ index: Int) {
        let flag = WhoopCodec.flags[index]
        let payload = WhoopCodec.setConfigPayload(name: flag.name, value: flag.value)
        if index == 0 {
            log("15 SET_CONFIG 0x78, los FLAGS de judes.club, en ese orden.", force: true)
        }
        send(.flag(index), command(0x78, b3: 0x01, payload: payload))
    }

    private func armRangeTimeout() {
        rangeTimeout?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.phase == .waitRange else { return }
            self.fail("GET_DATA_RANGE no contestó. No envié SEND_HISTORICAL.")
        }
        rangeTimeout = work
        ble.asyncAfter(deadline: .now() + 15, execute: work)
    }

    private func armStall() {
        stallTimeout?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.phase == .offload else { return }
            self.model.detail = "Lleva un rato sin marcos nuevos. Lo no confirmado sigue en la pulsera."
            self.publish(force: true)
        }
        stallTimeout = work
        ble.asyncAfter(deadline: .now() + 30, execute: work)
    }

    private func fail(_ message: String) {
        stopped = true
        phase = .finished
        outbound.removeAll()
        model.syncing = false
        model.status = message
        model.detail = "Lo que no se confirmó sigue en la pulsera."
        setIdle(false)
        log(message, force: true)
        refreshReady()
    }

    private func finish(message: String) {
        phase = .finished
        model.syncing = false
        model.status = message
        setIdle(false)
        try? store?.synchronize()
        log(message, force: true)
        refreshReady()
        pump()
    }

    private func handle(frame: [UInt8]) {
        let decoded = WhoopCodec.decodePrivate(frame)
        show(decoded)
        if model.syncing {
            guard persist(decoded, raw: frame) else {
                chunkFailed = true
                fail("No pude guardar un marco. No confirmé el cursor.")
                return
            }
        }
        if decoded.kind == "command-response" { noteCommand(frame) }
        if decoded.kind == "raw" || decoded.kind == "r20" { logRaw(frame) }
        guard model.syncing, !chunkFailed else { return }
        if decoded.kind == "history-end", phase == .offload, let cursor = decoded.cursor {
            do { try store?.synchronize() } catch {
                chunkFailed = true
                fail("El archivo no quedó en disco. No confirmé el cursor.")
                return
            }
            let current = seq
            guard let ack = WhoopCodec.command(cmd: 0x17, seq: current, b3: 0x01, payload: cursor) else { return }
            seq = seq &+ 1
            outbound.append((.ack, Data(ack)))
            pump()
        } else if decoded.kind == "history-complete" {
            finish(message: "La pulsera cerró el historial. Trozos confirmados: \(model.ackedChunks). Guardados: \(model.saved).")
        }
    }

    private func noteCommand(_ frame: [UInt8]) {
        guard frame.count >= 13 else { return }
        let cmd = frame[10]
        let result = frame[12]
        log(String(format: "Respuesta cmd 0x%02X result %d", cmd, result), force: true)
        if cmd == 0x91, result == 1, frame.count >= 18 {
            let tenths = Int(WhoopCodec.u32(frame, 14))
            if model.battery == nil { model.battery = tenths / 10 }
            if frame.count >= 111 {
                let suffix = Int(WhoopCodec.u32(frame, 107))
                model.helloFirmware = "\(frame[104]).\(frame[105]).\(frame[106]).\(suffix)"
            }
            publish()
        }
        guard phase == .waitRange, cmd == 0x22, frame[11] == rangeSeq else { return }
        if result == 2 {
            log("GET_DATA_RANGE pendiente", force: true)
            return
        }
        rangeTimeout?.cancel()
        if result == 1 {
            phase = .offload
            guard let historical = command(0x16, b3: 0x00) else { return }
            send(.historical, historical)
        } else {
            fail("GET_DATA_RANGE resultó \(result). No envié SEND_HISTORICAL.")
        }
    }

    private func show(_ decoded: WhoopCodec.Frame) {
        let now = Date()
        switch decoded.kind {
        case "packet40":
            if let hr = decoded.fields["hr"] as? Int {
                rememberHeart(hr, source: "paquete 40", at: now)
            }
            if let rr = decoded.fields["rrMs"] as? [Int], !rr.isEmpty {
                rememberRR(rr, source: "paquete 40", at: now)
            }
        case "r22":
            if let hr = decoded.fields["hr"] as? Int {
                rememberHeart(hr, source: "r22", at: now)
            }
            if let hr2 = decoded.fields["hr2"] as? Int { model.heartRate2 = hr2 }
            if let accel = decoded.fields["accelG"] as? [Double], accel.count == 3 {
                rememberMotion(accel, unit: "g", source: "r22", at: now)
            }
        case "r18":
            if let hr = decoded.fields["hr"] as? Int {
                rememberHeart(hr, source: "R18", at: now)
            }
            if let rr = decoded.fields["rrMs"] as? [Int], !rr.isEmpty {
                rememberRR(rr, source: "R18", at: now)
            }
            if let gravity = decoded.fields["gravityG"] as? [Double], gravity.count == 3 {
                rememberMotion(gravity, unit: "g", source: "R18 gravedad", at: now)
            }
            if let skin = decoded.fields["skinTempC"] as? Double {
                model.skinText = String(format: "%.2f °C", skin)
                model.skinSource = "R18, crudo/100"
            } else if let raw = decoded.fields["skinTempRaw"] as? Int {
                model.skinText = "crudo \(raw)"
                model.skinSource = "R18 fuera de 5–45 °C"
            }
        case "r21":
            if let rows = decoded.fields["accelRaw"] as? [[Int]], let first = rows.first, first.count == 3 {
                model.motionText = "\(first[0]), \(first[1]), \(first[2]) cuentas"
                model.motionSource = "R21, escala no confirmada en este firmware"
                latestMotion = (model.motionText, model.motionSource, now)
            }
        case "r26":
            if let motion = decoded.fields["summaryMotionRaw"] as? Double {
                model.motionText = String(format: "%.3f sin unidad", motion)
                model.motionSource = "R26, conversión no publicada"
            }
        case "event":
            if let event = decoded.fields["event"] as? Int {
                log("Evento \(event)", force: false)
            }
        default:
            break
        }
        if phase == .offload { armStall() }
        publish()
    }

    private func rememberHeart(_ bpm: Int, source: String, at: Date) {
        if let latest = latestHeart, latest.at > at { return }
        latestHeart = (bpm, source, at)
        model.heartRate = bpm
        model.heartSource = source
    }

    private func rememberRR(_ values: [Int], source: String, at: Date) {
        if let latest = latestRR, latest.at > at { return }
        let text = values.map { "\($0) ms" }.joined(separator: "  ")
        latestRR = (text, source, at)
        model.rrText = text
        model.rrSource = source
    }

    private func rememberMotion(_ values: [Double], unit: String, source: String, at: Date) {
        if let latest = latestMotion, latest.at > at { return }
        let text = values.map { String(format: "%.2f", $0) }.joined(separator: ", ") + " " + unit
        latestMotion = (text, source, at)
        model.motionText = text
        model.motionSource = source
    }

    private func persist(_ decoded: WhoopCodec.Frame, raw: [UInt8]) -> Bool {
        guard let store else { return false }
        var object = decoded.fields
        object["kind"] = decoded.kind
        object["receivedAt"] = timestamp()
        object["raw"] = Hex.encode(raw)
        do {
            try store.append(object)
            model.saved = store.records
            return true
        } catch {
            return false
        }
    }

    private func log(_ line: String, force: Bool) {
        model.log.append(line)
        if model.log.count > 40 { model.log.removeFirst(model.log.count - 40) }
        publish(force: force)
    }

    private func logRaw(_ frame: [UInt8]) {
        let now = Date()
        if now.timeIntervalSince(lastRawLog) < 0.25 { return }
        lastRawLog = now
        log("sin mapa  \(Hex.preview(frame))", force: false)
    }

    private func timestamp() -> String {
        ISO8601DateFormatter().string(from: Date())
    }

    private func publish(force: Bool = false) {
        if !force {
            guard !publishScheduled else { return }
            publishScheduled = true
            ble.asyncAfter(deadline: .now() + .milliseconds(200)) { [weak self] in
                guard let self else { return }
                self.publishScheduled = false
                let copy = self.model
                DispatchQueue.main.async { self.snap = copy }
            }
            return
        }
        let copy = model
        DispatchQueue.main.async { [weak self] in self?.snap = copy }
    }

    private func refreshReady() {
        let notifies = privateNotifies.allSatisfy { notifyOK.contains($0) }
        model.canSync = notifies && commandChar != nil && !authBlocked && model.selfCheck == nil && !model.syncing && model.connected
        if model.canSync {
            model.status = "Conectado a WHOOP DS"
            model.detail = "El pulso de arriba puede llegar sin el canal privado. El historial usa el bono de la app oficial, y eso no queda probado hasta esta ejecución."
        }
        publish(force: true)
    }

    private func setIdle(_ disabled: Bool) {
        DispatchQueue.main.async { UIApplication.shared.isIdleTimerDisabled = disabled }
    }

    private func describe(_ error: Error) -> String {
        let ns = error as NSError
        if ns.domain == CBATTErrorDomain {
            switch CBATTError.Code(rawValue: ns.code) {
            case .insufficientAuthentication:
                return "Código 5, authentication is insufficient. El bono de la app oficial no alcanzó."
            case .insufficientEncryption:
                return "Código 15, encryption is insufficient. El bono de la app oficial no alcanzó."
            default:
                return "Error Bluetooth \(ns.code)"
            }
        }
        return error.localizedDescription
    }

    private func text(_ data: Data) -> String {
        String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .controlCharacters.union(.whitespacesAndNewlines)) ?? ""
    }

    private func scan() {
        let known = central.retrieveConnectedPeripherals(withServices: [serviceID, heartService])
        if let found = known.first(where: { nameMatches($0.name) }) {
            connect(found)
            return
        }
        central.scanForPeripherals(withServices: nil, options: nil)
        model.status = "Buscando WHOOP DS"
        publish(force: true)
    }

    private func nameMatches(_ name: String?) -> Bool {
        name?.trimmingCharacters(in: .whitespacesAndNewlines).caseInsensitiveCompare("WHOOP DS") == .orderedSame
    }

    private func connect(_ peripheral: CBPeripheral) {
        self.peripheral = peripheral
        peripheral.delegate = self
        central.stopScan()
        model.status = "Conectando con WHOOP DS"
        model.connected = false
        publish(force: true)
        central.connect(peripheral, options: nil)
    }
}

extension WhoopSession: CBCentralManagerDelegate, CBPeripheralDelegate {
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        guard model.selfCheck == nil else { return }
        if central.state == .poweredOn {
            scan()
        } else {
            model.status = "Bluetooth no está listo"
            model.connected = false
            model.canSync = false
            publish(force: true)
        }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral, advertisementData: [String: Any], rssi RSSI: NSNumber) {
        let local = advertisementData[CBAdvertisementDataLocalNameKey] as? String
        guard nameMatches(peripheral.name ?? local) else { return }
        connect(peripheral)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        model.connected = true
        model.status = "Leyendo servicios"
        authBlocked = false
        notifyOK.removeAll()
        commandChar = nil
        assemblers.removeAll()
        publish(force: true)
        peripheral.discoverServices([serviceID, heartService, batteryService, infoService])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        model.status = "No conectó"
        model.detail = error.map(describe) ?? ""
        model.connected = false
        publish(force: true)
        if central.state == .poweredOn { scan() }
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        let wasSyncing = model.syncing
        stopped = true
        model.connected = false
        model.syncing = false
        model.canSync = false
        commandChar = nil
        setIdle(false)
        model.status = wasSyncing ? "Se cortó el enlace durante la descarga" : "Se cortó el enlace"
        model.detail = "Lo que no se confirmó sigue en la pulsera."
        if let error { log(describe(error), force: true) }
        publish(force: true)
        if !userClosed, central.state == .poweredOn, model.selfCheck == nil { scan() }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        if let error {
            model.detail = describe(error)
            publish(force: true)
            return
        }
        peripheral.services?.forEach { peripheral.discoverCharacteristics(nil, for: $0) }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        if let error {
            model.detail = describe(error)
            publish(force: true)
            return
        }
        for characteristic in service.characteristics ?? [] {
            let id = characteristic.uuid
            if id == fd4b(2) {
                if characteristic.properties.contains(.write) {
                    commandChar = characteristic
                } else {
                    model.detail = "fd4b0002 no acepta write-with-response. No uso write-without-response."
                }
            }
            if id == heartRateID || privateNotifies.contains(id) || id == batteryID {
                if characteristic.properties.contains(.notify) || characteristic.properties.contains(.indicate) {
                    peripheral.setNotifyValue(true, for: characteristic)
                }
            }
            if id == batteryID || id == modelID || id == serialID || id == firmwareID || id == hardwareID {
                if characteristic.properties.contains(.read) {
                    peripheral.readValue(for: characteristic)
                }
            }
        }
        refreshReady()
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            let message = "\(characteristic.uuid.uuidString): \(describe(error))"
            log(message, force: true)
            if privateNotifies.contains(characteristic.uuid) {
                authBlocked = true
                model.status = "La pulsera rechazó el canal privado"
                model.detail = describe(error)
            }
        } else if characteristic.isNotifying, privateNotifies.contains(characteristic.uuid) {
            notifyOK.insert(characteristic.uuid)
        }
        refreshReady()
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            log(describe(error), force: true)
            if privateNotifies.contains(characteristic.uuid) || characteristic.uuid == fd4b(2) {
                authBlocked = true
                model.status = "Lectura rechazada"
                model.detail = describe(error)
                refreshReady()
            }
            return
        }
        let bytes = Array(characteristic.value ?? Data())
        switch characteristic.uuid {
        case heartRateID:
            if let hr = WhoopCodec.decodeHeartRate(bytes) {
                let now = Date()
                rememberHeart(hr.bpm, source: "2A37", at: now)
                if !hr.rrMs.isEmpty { rememberRR(hr.rrMs, source: "2A37", at: now) }
                model.contact = hr.contact == nil ? "" : (hr.contact == true ? "contacto" : "sin contacto")
                publish()
            }
        case batteryID:
            if let value = bytes.first {
                model.battery = Int(value)
                publish()
            }
        case modelID:
            model.model = text(Data(bytes))
            publish()
        case serialID:
            model.serial = text(Data(bytes))
            model.serialNote = model.serial == "5AM0177536" ? "Serie esperada" : "La serie leída no es 5AM0177536"
            publish()
        case firmwareID:
            model.firmware = text(Data(bytes))
            publish()
        case hardwareID:
            model.hardware = text(Data(bytes))
            publish()
        default:
            guard privateNotifies.contains(characteristic.uuid) else { return }
            let frames = assembler(for: characteristic.uuid).feed(bytes)
            if frames.isEmpty, !bytes.isEmpty, loggedKinds.insert("fragment").inserted {
                log("Fragmento \(Hex.preview(bytes))", force: false)
            }
            for frame in frames { handle(frame: frame) }
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        let finished = inflight
        inflight = nil
        if let error {
            authBlocked = true
            fail(describe(error))
            return
        }
        guard let finished else {
            pump()
            return
        }
        if case .ack = finished {
            advance(after: finished)
            pump()
            return
        }
        guard !stopped else { return }
        ble.asyncAfter(deadline: .now() + .milliseconds(40)) { [weak self] in
            guard let self, !self.stopped else { return }
            self.advance(after: finished)
            self.pump()
        }
    }

    private func assembler(for uuid: CBUUID) -> FrameReassembler {
        if let existing = assemblers[uuid] { return existing }
        let created = FrameReassembler()
        assemblers[uuid] = created
        return created
    }

}
