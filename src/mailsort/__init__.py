"""mailsort : tri automatique des e-mails Gmail par Jev."""

__version__ = "0.1.0"

import logging as _logging

# Sans journal configuré (commandes interactives), les messages du logger « mailsort » ne sont
# pas recopiés sur la console : la commande affiche déjà ce qu'il faut.
_logging.getLogger("mailsort").addHandler(_logging.NullHandler())
