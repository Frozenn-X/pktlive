import os
import socket
import struct
import multiprocessing as mp
from typing import NoReturn

from .._paths import BRONZE_DIR

# --- Configuration Haute Performance ---
# On utilise AF_PACKET pour lire directement les trames Ethernet (Linux uniquement)
# ETH_P_ALL = 3 (Tous les protocoles)
ETH_P_ALL = 3

def raw_capture_worker(worker_id: int, queue: mp.Queue) -> NoReturn:
    """
    Worker ultra-rapide utilisant des Raw Sockets.
    Complexité : O(1) - Lecture binaire directe.
    """
    # Création du socket brut
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    sock.bind(("eth0", 0))

    print(f"[Worker {worker_id}] Listening on eth0...")

    while True:
        # On lit le buffer binaire (max 65535 bytes pour un MTU standard)
        packet, _ = sock.recvfrom(65535)
        
        # Guard Clause: On ne traite que l'essentiel (Header IP commence à l'octet 14)
        # On extrait juste IP Source, Destination et Protocole en binaire (très rapide)
        ip_header = packet[14:34]
        if len(ip_header) < 20: continue

        # Unpack binaire : Pas d'objets complexes ici !
        iph = struct.unpack('!BBHHHBBH4s4s', ip_header)
        src_ip = socket.inet_ntoa(iph[8])
        dst_ip = socket.inet_ntoa(iph[9])
        
        # Envoi vers le process d'écriture (Batcher)
        queue.put((src_ip, dst_ip, len(packet)))

def batch_writer(queue: mp.Queue, output_path: str):
    """Processus dédié à l'écriture disque pour ne pas ralentir la capture."""
    buffer = []
    while True:
        item = queue.get()
        buffer.append(item)
        
        if len(buffer) >= 5000:  # Flush par gros blocs pour Databricks
            # Logique d'écriture NDJSON ici...
            buffer = []

if __name__ == "__main__":
    ctx = mp.get_context('spawn')
    traffic_queue = ctx.Queue()

    # On lance 1 process par cœur CPU pour le sniffing
    for i in range(os.cpu_count()):
        p = ctx.Process(target=raw_capture_worker, args=(i, traffic_queue))
        p.start()

    # On lance le writer
    writer = ctx.Process(target=batch_writer, args=(traffic_queue, str(BRONZE_DIR)))
    writer.start()