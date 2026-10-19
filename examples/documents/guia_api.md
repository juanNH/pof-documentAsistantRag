# Guía de límites de la API

Cuando se supera la cuota, la API responde con el código HTTP 429 Too Many Requests. El cliente debe esperar el tiempo indicado en el encabezado Retry-After antes de volver a intentar la solicitud.

El encabezado X-Request-ID permite localizar una solicitud en los registros de soporte.
