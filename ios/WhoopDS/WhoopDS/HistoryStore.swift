import Foundation

/// Append-only archive for a later morning-recovery pass. This file does not score anything.
/// A HISTORY_END ack is sent only after `synchronize()` succeeds for the bytes already appended.
final class HistoryStore {
    let url: URL
    private let handle: FileHandle
    private(set) var records = 0

    init() throws {
        let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        url = directory.appendingPathComponent("whoop-history.jsonl")
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
    }

    func append(_ object: [String: Any]) throws {
        guard JSONSerialization.isValidJSONObject(object) else {
            throw CocoaError(.coderInvalidValue)
        }
        var line = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        line.append(0x0A)
        try handle.write(contentsOf: line)
        records += 1
    }

    func synchronize() throws {
        try handle.synchronize()
    }

    deinit {
        try? handle.close()
    }
}
