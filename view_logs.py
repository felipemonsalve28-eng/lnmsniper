#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import time
from pathlib import Path

LOG_FILE = "sniper_advanced.log"

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def main():
    path = Path(LOG_FILE)
    if not path.exists():
        print(f"❌ No se encontró el archivo de log: {LOG_FILE}")
        return

    print("Iniciando visor limpio de logs (Sin degradaciones de Edge)...")
    time.sleep(1)

    try:
        while True:
            clear_screen()
            print("=" * 62)
            print("     LNM SNIPER v7.3 · VISOR PRINCIPAL (SIN RUIDO)     ")
            print("=" * 62)
            
            if path.exists():
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    fsize = f.tell()
                    # Tomar un bloque más amplio para compensar las líneas filtradas
                    block = min(fsize, 32768)
                    f.seek(max(0, fsize - block), os.SEEK_SET)
                    lines = f.read().decode("utf-8", errors="ignore").splitlines()
                
                # Filtrar líneas molestas o redundantes (como las degradaciones de Edge)
                filtered_lines = []
                for line in lines:
                    if "[DEGRADACIÓN DE EDGE]" in line:
                        continue
                    filtered_lines.append(line)
                
                # Mostrar solo las últimas 20 líneas limpias para no saturar Termux
                for line in filtered_lines[-20:]:
                    if "[ERROR" in line or "🚨" in line or "KILL SWITCH" in line:
                        print(f"\033[91m{line}\033[0m") # Rojo para errores críticos
                    elif "⚠️" in line or "INSUFICIENTE" in line:
                        print(f"\033[93m{line}\033[0m") # Amarillo para advertencias o bloqueos
                    elif "✅" in line or "CONQUISTADA" in line or "SALUD API" in line or "Abierta" in line:
                        print(f"\033[92m{line}\033[0m") # Verde para éxito, aperturas o sincronizaciones
                    else:
                        print(line)
            else:
                print("Esperando registros en el log...")

            print("=" * 62)
            print(" Actualizando cada 3s · Presiona Ctrl+C para salir")
            time.sleep(3)
    except KeyboardInterrupt:
        print("\nVisor cerrado correctamente.")

if __name__ == "__main__":
    main()
