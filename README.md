# midm-pi-keyboard

Un Raspberry Pi 5 qui se fait passer pour un **clavier Bluetooth** auprès d'un PC, et qui peut **relayer un clavier branché sur le Pi** (USB ou récepteur sans fil) vers ce PC.

Testé avec : Raspberry Pi 5, Raspberry Pi OS (Debian 12), BlueZ 5.66, noyau 6.12, PC Windows 11 avec radio Intel.

## Comment ça marche

```
clavier USB ──evdev──▶ btkbd (Pi) ──L2CAP PSM 17/19 (HID classique BR/EDR)──▶ PC
```

- `pi/btkbd.py` : démon Python. Il enregistre un profil HID auprès de BlueZ (enregistrement SDP d'un clavier), écoute sur les PSM 17 (contrôle) et 19 (interruption) et envoie des rapports HID de 8 octets.
- `pi/bluetooth-noinput.conf` : lance `bluetoothd --noplugin=input`. Sans cela, le plugin `input` de BlueZ occupe les PSM 17/19. **Conséquence : le Pi ne peut plus utiliser un clavier Bluetooth comme hôte.**
- `pi/btkbd-send` : client en ligne de commande (socket Unix `/run/btkbd.sock`).
- Relais : toute entrée `evdev` qui ressemble à un clavier est accaparée (`EVIOCGRAB`) tant qu'un PC est connecté, et ses touches sont envoyées au PC. Sans PC connecté, le clavier reste libre. Les voyants (Verr Maj, Verr Num) sont recopiés depuis le PC.

## Installation (sur le Pi)

```bash
sudo apt install python3-dbus python3-gi python3-evdev
sudo ./pi/install.sh
```

> `install.sh` reprend les étapes faites à la main lors du développement ; le script lui-même n'a pas été exécuté de bout en bout. Il est idempotent et sauvegarde `main.conf` en `main.conf.orig`.

## Appairage

Le Pi n'est visible que pendant une fenêtre de 2 minutes, et n'accepte que des appareils déjà appairés :

```bash
sudo btkbd-send pairing on
```

Puis, sur le PC Windows : *Paramètres → Bluetooth et appareils → Ajouter un appareil → « Raspberry Pi Keyboard »*.
Ou sans interface : `windows/btpair.ps1 -Action pair -Mac <adresse Bluetooth du Pi>` (accepte la confirmation côté PC).

Ensuite, marquez le PC comme appareil de confiance pour que le Pi se reconnecte seul :

```bash
bluetoothctl trust <adresse Bluetooth du PC>
```

## Utilisation

```bash
sudo btkbd-send status
sudo btkbd-send text "hello"
sudo btkbd-send key enter
sudo btkbd-send key r --mods gui
sudo btkbd-send relay off      # suspendre le relais (rend le clavier au Pi)
```

## Tests

Voir `windows/` et `tests/`. Ils se lancent depuis le PC et pilotent le Pi en SSH (`-PiTarget utilisateur@hôte`).

| Script | Rôle | Focus nécessaire |
| --- | --- | --- |
| `windows/rawtest.ps1` | Le Pi envoie Ctrl/Maj seuls ; une fenêtre invisible les lit avec *Raw Input* et indique le périphérique d'origine. `-PiScriptName uinput_mod_test.py` teste toute la chaîne avec un clavier virtuel. | non |
| `windows/typetest.ps1` | Le Pi tape les 95 caractères ASCII imprimables dans une fenêtre de test, relus et comparés. | oui (la fenêtre doit avoir le focus ; sinon le script s'arrête sans rien envoyer) |
| `windows/btpair.ps1` | Appairer / désappairer / lister un appareil précis, par adresse MAC. | non |

## Limites connues

- **Les codes envoyés sont des positions de touches.** La disposition affichée est celle du PC : un clavier AZERTY branché sur le Pi donnera du QWERTY sur un Windows en US.
- Le descripteur HID déclare les usages `0x00`–`0x65` : F13–F24 et les touches multimédia ne sont pas relayées.
- Si un autre programme a déjà accaparé le clavier (`EBUSY`), le relais le signale une fois dans le journal et réessaie toutes les 5 s.
- Après un redémarrage de Bluetooth, le Pi peut rester « appairable » ; l'agent d'appairage refuse néanmoins tout appairage hors fenêtre ouverte (ce refus n'a pas été testé).
- Le HID classique est utilisé de préférence au BLE (HOGP) car il fonctionne de façon fiable avec Windows.
