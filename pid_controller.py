"""
Logica di riferimento per il PID CTRL di Figura 1 (AntiSEL_System_Description).

Riscaldatore unidirezionale (solo riscaldamento, mai raffreddamento): l'uscita
e' un duty cycle PWM 0-100%, quindi il controllore usa anti-windup a clamp
condizionale sull'integrale per evitare overshoot quando l'attuatore satura.
Guadagni fissi, aggiornamento a ~1 Hz: coerenti con la costante di tempo
termica del banco (secondi-minuti) e col polling GET TEMP gia' previsto nel
placeholder RTU/PID della dashboard.

Riferimento per l'implementazione firmware reale del PID CTRL; non e'
eseguito dalla dashboard (che oggi si limita a monitorare, non a controllare).
"""


class HeaterPID:
    def __init__(self, kp, ki, kd=0.0, out_min=0.0, out_max=100.0, dt=1.0):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.out_min, self.out_max = out_min, out_max
        self.dt = dt
        self.reset()

    def reset(self):
        self._integral = 0.0
        self._prev_error = None

    def update(self, setpoint, measurement):
        """Calcola il nuovo duty cycle [%] dato il setpoint e la misura RTU."""
        error = setpoint - measurement
        d_error = 0.0 if self._prev_error is None else (error - self._prev_error) / self.dt
        self._prev_error = error

        integral_candidate = self._integral + self.ki * error * self.dt
        output_unclamped = self.kp * error + integral_candidate + self.kd * d_error
        output = max(self.out_min, min(self.out_max, output_unclamped))

        # Anti-windup a clamp condizionale: aggiorna l'integrale solo se
        # l'uscita non e' saturata nella direzione che l'errore spingerebbe.
        saturated_high = output_unclamped > self.out_max and error > 0
        saturated_low = output_unclamped < self.out_min and error < 0
        if not (saturated_high or saturated_low):
            self._integral = integral_candidate

        return output
