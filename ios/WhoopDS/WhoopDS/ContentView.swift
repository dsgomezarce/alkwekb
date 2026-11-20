import SwiftUI

struct ContentView: View {
    @ObservedObject var session: WhoopSession
    @State private var confirm = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                banner
                status
                if let problem = session.snap.selfCheck {
                    Text(problem)
                        .font(.footnote)
                        .foregroundStyle(.red)
                }
                heart
                metric("Intervalos R-R", session.snap.rrText, session.snap.rrSource)
                metric("Batería", batteryText, "")
                metric("Movimiento", session.snap.motionText, session.snap.motionSource)
                metric("Piel", session.snap.skinText, session.snap.skinSource.isEmpty ? "Solo si llega un R18" : session.snap.skinSource)
                device
                sync
                log
            }
            .padding(20)
        }
        .background(Color(red: 0.06, green: 0.07, blue: 0.06).ignoresSafeArea())
        .preferredColorScheme(.dark)
        .confirmationDialog(
            "Whoop no puede estar conectado. Al confirmar el historial, la pulsera suelta esos intervalos y la nube no los puntúa.",
            isPresented: $confirm,
            titleVisibility: .visible
        ) {
            Button("Descargar historial", role: .destructive) { session.startHistory() }
            Button("Cancelar", role: .cancel) {}
        }
    }

    private var banner: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Un solo host")
                .font(.headline)
            Text("Cierra la app oficial de Whoop antes de sincronizar. No puede quedar conectada. Cuando un trozo del historial se confirma, sale de la pulsera: la nube de Whoop no va a puntuar esos intervalos.")
                .font(.subheadline)
        }
        .foregroundStyle(Color(red: 1, green: 0.93, blue: 0.8))
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(red: 0.42, green: 0.24, blue: 0.08), in: RoundedRectangle(cornerRadius: 14))
    }

    private var status: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(session.snap.status)
                .font(.title3.weight(.semibold))
            if !session.snap.detail.isEmpty {
                Text(session.snap.detail)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            if !session.snap.contact.isEmpty {
                Text(session.snap.contact)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var heart: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(session.snap.heartRate.map(String.init) ?? "—")
                .font(.system(size: 72, weight: .semibold, design: .rounded))
                .monospacedDigit()
            Text(session.snap.heartSource.isEmpty ? "pulsaciones por minuto" : "lpm · \(session.snap.heartSource)")
                .foregroundStyle(.secondary)
            if let second = session.snap.heartRate2 {
                Text("Segundo canal \(second) lpm")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func metric(_ title: String, _ value: String, _ source: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.title3.monospacedDigit())
            if !source.isEmpty {
                Text(source)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var batteryText: String {
        session.snap.battery.map { "\($0)%" } ?? "—"
    }

    private var device: some View {
        VStack(alignment: .leading, spacing: 4) {
            labeled("Modelo", session.snap.model)
            labeled("Serie", [session.snap.serial, session.snap.serialNote].filter { !$0.isEmpty }.joined(separator: " · "))
            labeled("Firmware", session.snap.firmware)
            labeled("Hardware", session.snap.hardware)
            if !session.snap.helloFirmware.isEmpty {
                labeled("Hello", session.snap.helloFirmware)
            }
            Text("Los mapas de R18, R21 y el paquete 40 están publicados con firmware 50.42.1.0 como línea base.")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    private func labeled(_ name: String, _ value: String) -> some View {
        HStack {
            Text(name).foregroundStyle(.secondary)
            Spacer()
            Text(value.isEmpty ? "—" : value)
        }
        .font(.subheadline)
    }

    private var sync: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button(session.snap.syncing ? "Descargando…" : "Descargar historial") {
                confirm = true
            }
            .buttonStyle(.borderedProminent)
            .tint(Color(red: 0.55, green: 0.22, blue: 0.16))
            .disabled(!session.snap.canSync)
            Button("Cerrar enlace") { session.disconnect() }
                .disabled(!session.snap.connected)
            if !session.snap.connected {
                Button("Buscar WHOOP DS") { session.searchAgain() }
            }
            Text("Guardados en esta descarga: \(session.snap.saved). Trozos confirmados: \(session.snap.ackedChunks).")
            Text("El archivo queda en Archivos, dentro de Whoop DS: \(session.snap.fileName). Ahí está el historial para calcular la recuperación después. Esta pantalla no la calcula.")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
    }

    private var log: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Hex y comandos")
                .font(.caption)
                .foregroundStyle(.secondary)
            if session.snap.log.isEmpty {
                Text("Los marcos sin un mapa publicado aparecen aquí en hex.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ForEach(Array(session.snap.log.enumerated()), id: \.offset) { _, line in
                Text(line)
                    .font(.caption2.monospaced())
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}
