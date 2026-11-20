import SwiftUI

@main
struct WhoopDSApp: App {
    @StateObject private var session = WhoopSession()

    var body: some Scene {
        WindowGroup {
            ContentView(session: session)
        }
    }
}
