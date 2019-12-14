import json, threading, socket, sys
from player import Player
from payload import move
from typing import *


class Room:
    def __init__(self, ip, port, room_map):
        self.players:  List[Optional[Player]] = []
        self.walls: set = set()

        self.max_players = 100
        self.tick_rate = 100

        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.bind((ip, port))
        self.s.listen(16)

        with open(room_map) as data:
            mapData = json.load(data)
            self.walls = mapData['walls']
            self.width, self.height = mapData['size']

        # Create player spots in game object
        for index in range(0, self.max_players):
            self.players.append(None)

    def accept_clients(self) -> None:
        while True:
            client_socket, address = self.s.accept()

            player_id: int = -1
            for index in range(0, len(self.players)):
                if self.players[index] is None:
                    player_id = index
                    break

            if player_id == -1:
                client_socket.send(bytes("full;", 'utf-8'))
                client_socket.close()
                print("Connection from %s rejected." % address)
            else:
                print("Connection from %s. Assigning to player %d" % (address, player_id))
                init_data = {
                  'id': player_id,
                  'w': self.width,
                  'h': self.height,
                  'walls': self.walls,
                  't': self.tick_rate
                }

                client_socket.send(bytes(json.dumps(init_data) + ";", 'utf-8'))
                self.players[player_id] = Player(client_socket, init_data)

                threading.Thread(target=self.listen, args=(player_id, ), daemon=True).start()

    def listen(self, player_id) -> None:
        while True:
            player = self.players[player_id]
            data = ''
            try:
                while True:
                    data += player.client_socket.recv(1024).decode('utf-8')

                    if data[-1] == ';':
                        break

            except Exception as e:
                print("Player %d: Disconnected. %s" % (player_id, str(e)))
                player.client_socket.close()
                self.players[player_id] = None
                break

            try:
                data = json.loads(data[:-1])
                action: str = data['a']
                payload: str = data['p']

                print("Received data from player %d: Action=%s, Payload=%s" % (player_id, action, payload))

                pos = player.state['pos']

                # Move
                if action == 'm':
                    if payload == move.Direction.UP and pos['y'] - 1 > 0 and [pos['x'], pos['y'] - 1] not in self.walls:
                        pos['y'] -= 1
                    if payload == move.Direction.RIGHT and pos['x'] + 1 < self.width - 1 and [pos['x'] + 1, pos['y']] not in self.walls:
                        pos['x'] += 1
                    if payload == move.Direction.DOWN and pos['y'] + 1 < self.height - 1 and [pos['x'], pos['y'] + 1] not in self.walls:
                        pos['y'] += 1
                    if payload == move.Direction.LEFT and pos['x'] - 1 > 0 and [pos['x'] - 1, pos['y']] not in self.walls:
                        pos['x'] -= 1

            except Exception as e:
                print(e, file=sys.stderr)
                pass

    def update_clients(self) -> None:
        players = []

        for index in range(0, len(self.players)):
            player = self.players[index]

            players.append(player.state if player else None)

        for player in self.players:
            if player:
                player.client_socket.send(bytes(json.dumps({'p': players}) + ";", 'utf-8'))
