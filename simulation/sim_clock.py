# -*- coding: utf-8 -*-
"""
simulation/sim_clock.py
Reloj determinista de simulación desacoplado del tiempo real del sistema.
Permite avance rápido tick por tick o vela por vela con precisión a nivel de microsegundo,
evaluación exacta de periodicidad (funding 8h, ciclos 1h/4h) y reemplazo directo de time.time().
"""
from typing import Optional, Callable


class SimClock:
    """
    Reloj virtual determinista para backtesting y entrenamiento offline acelerado.
    """

    def __init__(self, start_timestamp: float = 1700000000.0):
        self._current_time: float = float(start_timestamp)
        self._initial_time: float = float(start_timestamp)

    def time(self) -> float:
        """Devuelve el timestamp simulado actual (reemplazo de time.time())."""
        return self._current_time

    def set_time(self, timestamp: float) -> None:
        """Establece explícitamente el tiempo del reloj virtual."""
        self._current_time = float(timestamp)

    def advance(self, seconds: float) -> float:
        """
        Avanza el reloj virtual en el número de segundos especificado.
        Retorna el nuevo timestamp actual.
        """
        if seconds < 0:
            raise ValueError(f"No se puede retroceder el reloj: seconds={seconds}")
        self._current_time += float(seconds)
        return self._current_time

    def sleep(self, seconds: float) -> None:
        """
        Emula time.sleep(seconds) avanzando instantáneamente el reloj virtual
        sin bloquear el hilo de ejecución de la CPU.
        """
        self.advance(seconds)

    def elapsed_seconds(self) -> float:
        """Segundos totales transcurridos desde el inicio de la simulación."""
        return self._current_time - self._initial_time

    def is_cycle_due(self, interval_seconds: float, last_ts: Optional[float]) -> bool:
        """
        Determina con rigor matemático si se ha cruzado la frontera de un ciclo
        periódico (ej. velas de 1h = 3600s, ciclos de 4h = 14400s, funding = 28800s).
        Usa cálculo por buckets: floor(t / interval) > floor(last_ts / interval).
        """
        if interval_seconds <= 0:
            return True
        if last_ts is None:
            return True
        last_bucket = int(float(last_ts) // float(interval_seconds))
        curr_bucket = int(self._current_time // float(interval_seconds))
        return curr_bucket > last_bucket

    def __repr__(self) -> str:
        return f"<SimClock current_time={self._current_time:.3f} elapsed={self.elapsed_seconds():.1f}s>"
