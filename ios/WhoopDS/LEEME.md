# Whoop DS

App de iPhone para el WHOOP MG `WHOOP DS`. Lee pulso, intervalos R-R, batería y los campos que Wilson y NOOP publican (movimiento y temperatura de piel cuando el marco trae ese mapa). Lo que no tiene mapa sale en hex. El historial se guarda en `whoop-history.jsonl` para calcular la recuperación después; esta app no la calcula.

El enlace con el bono de la app oficial no está probado hasta que corras esto en el iPhone.

## Un solo host

Cierra la app oficial de Whoop antes de descargar. No puede quedar conectada. Al confirmar un trozo, la pulsera lo suelta y la nube de Whoop no va a puntuar esos intervalos. No se envía alarma, buzz, reinicio, DFU ni trim.

## En el iPhone

1. No desvincules el Whoop en la app oficial. El bono tiene que seguir en el iPhone.
2. Cierra Whoop del todo, fuera del conmutador de apps.
3. En el Mac, abre `ios/WhoopDS/WhoopDS.xcodeproj`. Elige tu iPhone, firma con tu equipo y pulsa Run.
4. Acepta Bluetooth. La app busca el nombre `WHOOP DS`.
5. Si el canal privado responde código 5 o 15, el bono no alcanzó. No desvincules nada: copia el texto de la pantalla.
6. Para el historial, con Whoop cerrado, confirma el aviso y pulsa Descargar historial. Deja esta app abierta y el teléfono despierto.
7. El archivo queda en Archivos → Whoop DS → `whoop-history.jsonl`.
8. Cuando termines, pulsa Cerrar enlace antes de volver a abrir Whoop.

Los bytes que se escriben están citados en `WhoopDS/WhoopCodec.swift`. `python3 ios/WhoopDS/protocol_vectors.py` comprueba el saludo publicado y el frame dorado de `enable_r22_packets`.
