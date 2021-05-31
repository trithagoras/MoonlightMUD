import random

import django
from django.db.utils import DataError
import rsa
from django.core.exceptions import ObjectDoesNotExist
from django.forms import model_to_dict
from twisted.internet.protocol import connectionDone
from twisted.protocols.basic import NetstringReceiver

from collections import deque
from networking import cryptography

from typing import *

from networking import packet
from networking.logger import Log
from server import models, pbkdf2
import maps


OOB = -32       # Out Of Bounds. All instances with y == OOB are awaiting to be respawned.


def get_dict_delta(before: dict, after: dict) -> dict:
    delta = {'id': before['id']}
    for k, v in after.items():
        if k == 'id':
            continue
        if v != before[k]:
            delta[k] = v

    return delta


def create_dict(model_type: str, model) -> dict:
    """
    Creates recursive dict to replace model_to_dict
    :param model_type: one of ('Instance', 'InventoryItem')
    :param model: a django.model
    :return:
    """
    if model_type == 'Instance':
        instancedict = model_to_dict(model)
        entdict = model_to_dict(model.entity)
        instancedict["entity"] = entdict
        return instancedict

    elif model_type == 'InventoryItem':
        cidict = model_to_dict(model)
        itemdict = model_to_dict(model.item)
        entdict = model_to_dict(model.item.entity)
        cidict["item"] = itemdict
        cidict["item"]["entity"] = entdict
        return cidict


class MoonlapseProtocol(NetstringReceiver):
    def __init__(self, server):
        self.server = server

        # Information specific to the player using this protocol
        self.username = ""
        self.player_instance: Optional[models.InstancedEntity] = None
        self.player_info: Optional[models.Player] = None
        self.roommap: Optional[maps.Room] = None
        self.logged_in = False
        self.client_pub_key: Optional[rsa.key.PublicKey] = None

        self.state = self.GET_ENTRY
        self.actionloop = None

        self.outgoing = deque()
        self.next_packet: Optional[packet.Packet] = None     # most recent packet from client to process next tick

        self.logger = Log()

        self.visible_instances: Set[models.InstancedEntity] = set()

    def connectionMade(self):
        self.server.connected_protocols.add(self)

    def connectionLost(self, reason=connectionDone):
        self.logout(packet.LogoutPacket(self.username))
        self.server.connected_protocols.remove(self)

    def stringReceived(self, string):
        # attempt to decrypt packet
        try:
            string = cryptography.decrypt(string, self.server.private_key)
        except Exception as e:
            self.debug(f"WARNING: Packet came through unencrypted")
            self.debug(str(e))
        p = packet.frombytes(string)
        self.debug(f"Received packet from my client {p}")
        self.next_packet = p

    def process_packet(self, p: packet.Packet):
        self.state(p)

    def GET_ENTRY(self, p: packet.Packet):
        if isinstance(p, packet.ClientKeyPacket):
            # We have the client's public key so now we can send some initial data
            self.client_pub_key = rsa.key.PublicKey(p.payloads[0].value, p.payloads[1].value)
            # Send the client the server's public key
            self.outgoing.append(packet.ClientKeyPacket(self.server.public_key.n, self.server.public_key.e))
            # Send the client some initial info it needs to know
            self.outgoing.append(packet.ServerTickRatePacket(self.server.tickrate))
            self.outgoing.append(packet.WelcomePacket(
                """Welcome to MoonlapseMUD\n ,-,-.\n/.( +.\\\n\ {. */\n `-`-'\n     Enjoy your stay ~"""))
        if isinstance(p, packet.LoginPacket):
            self.login_user(p)
        elif isinstance(p, packet.RegisterPacket):
            self.register_user(p)

    def login_user(self, p: packet.LoginPacket):
        username, password = p.payloads[0].value, p.payloads[1].value
        if not models.User.objects.filter(username=username):
            self.outgoing.append(packet.DenyPacket("I don't know anybody by that name"))
            return

        user = models.User.objects.get(username=username)
        player = models.Player.objects.get(user=user)

        if self.server.is_logged_in(player.pk):
            self.outgoing.append(packet.DenyPacket(f"{username} is already inhabiting this realm."))
            return

        if not pbkdf2.verify_password(user.password, password):
            self.outgoing.append(packet.DenyPacket("Incorrect password"))
            return

        # The user exists in the database so retrieve the player and entity objects
        self.username = user.username
        self.player_info = player
        self.player_instance = models.InstancedEntity.objects.get(entity=self.player_info.entity)
        self.player_instance = self.server.instances[self.player_instance.pk]

        self.outgoing.append(packet.OkPacket())
        self.move_rooms(self.player_instance.room.id)

    def register_user(self, p: packet.RegisterPacket):
        username, password = p.payloads[0].value, p.payloads[1].value

        if models.User.objects.filter(username=username):
            self.outgoing.append(packet.DenyPacket("Somebody else already goes by that name"))
            return

        password = pbkdf2.hash_password(password)

        # Save the new user
        user = models.User(username=username, password=password)
        try:
            user.save()
        except DataError as e:
            self.outgoing.append(packet.DenyPacket("Error. Value too long."))
            return

        # Create and save a new entity
        entity = models.Entity(typename='Player', name=username)
        entity.save()

        # Create and save a new instance
        initial_room = models.Room.objects.first()
        if not initial_room:
            self.outgoing.append(packet.DenyPacket("Error. Please try again later."))
            raise ObjectDoesNotExist("Initial room not loaded. Did you run manage.py loaddata data.json?")
        instance = models.InstancedEntity(entity=entity, room=initial_room, y=0, x=0)
        instance.save()

        # Create and save a new player
        player = models.Player(user=user, entity=entity)
        player.save()

        # Create and save a new bank for the player
        bank = models.Bank(player=player)
        bank.save()

        # adding instance to server
        self.server.instances[instance.pk] = instance

        self.outgoing.append(packet.OkPacket())

    def logout(self, p: packet.LogoutPacket):
        username = p.payloads[0].value
        if username == self.username:
            # Tell our client it's OK to log out
            self.outgoing.append(packet.OkPacket())

            # tell everyone we're leaving
            if self.player_instance:
                self.broadcast(packet.GoodbyePacket(self.player_instance.pk))

            self.logged_in = False
            self.player_instance = None
            self.player_info = None
            self.roommap = None
            self.username = ""
            self.visible_instances = set()
            self.state = self.GET_ENTRY

            if self.actionloop:
                self.server.remove_deferred(self.actionloop)
                self.actionloop = None

    def PLAY(self, p: packet.Packet):
        if isinstance(p, packet.MovePacket):
            self.move(p)
        elif isinstance(p, packet.ChatPacket):
            self.chat(p)
        elif isinstance(p, packet.LogoutPacket):
            self.logout(p)
        elif isinstance(p, packet.GoodbyePacket):
            self.depart_other(p)
        elif isinstance(p, packet.ServerLogPacket):
            self.outgoing.append(p)
        elif isinstance(p, packet.GrabItemPacket):
            self.grab_item_here()
        elif isinstance(p, packet.DropItemPacket):
            self.drop_item(p)
        elif isinstance(p, packet.WeatherChangePacket):
            self.outgoing.append(p)

    def chat(self, p: packet.ChatPacket):
        """
        Broadcasts a chat message which includes this protocol's connected player name.
        Truncates to 80 characters. Cannot be empty.
        """
        message: str = p.payloads[0].value
        if message.strip() != '':
            message: str = f"{self.player_instance.entity.name} says: {message[:80]}"
            self.broadcast(packet.ServerLogPacket(message), include_self=True)
            self.logger.log(message)

    def add_item_to_inventory(self, item: models.Item, amt: int) -> int:
        """
        adds this item to inventory
        :param item:
        :param amt:
        :require: amt <= item.max_stack_amt
        :return: leftover (if inventory is full)
        """

        inv_items = models.InventoryItem.objects.filter(item=item, player=self.player_info)
        for inv_item in inv_items:
            if inv_item.amount == item.max_stack_amt:
                continue
            else:
                leftover = max((inv_item.amount + amt) - item.max_stack_amt, 0)
                inv_item.amount = min(item.max_stack_amt, inv_item.amount + amt)
                inv_item.save()
                self.outgoing.append(packet.ServerModelPacket('InventoryItem', create_dict('InventoryItem', inv_item)))

                while leftover > 0:
                    # if inventory is full
                    if len(models.InventoryItem.objects.filter(player=self.player_info)) == 30:
                        self.outgoing.append(packet.DenyPacket("Your inventory is full"))
                        return leftover

                    new_amt = min(item.max_stack_amt, leftover)
                    new_inv_item = models.InventoryItem(item=item, amount=new_amt, player=self.player_info)
                    new_inv_item.save()
                    self.outgoing.append(packet.ServerModelPacket('InventoryItem', create_dict('InventoryItem', new_inv_item)))
                    leftover -= new_amt
                return 0

        # if inventory is full
        if len(models.InventoryItem.objects.filter(player=self.player_info)) == 30:
            self.outgoing.append(packet.DenyPacket("Your inventory is full"))
            return amt

        new_inv_item = models.InventoryItem(item=item, amount=amt, player=self.player_info)
        new_inv_item.save()
        self.outgoing.append(packet.ServerModelPacket('InventoryItem', create_dict('InventoryItem', new_inv_item)))
        return 0

    def kill_instance(self, instance):
        """
        Not 'kill', but flag for respawn. e.g. grabbing item / mining rocks / killing goblin / etc.
        """
        self.broadcast(packet.GoodbyePacket(instance.pk), include_self=True)

        # a respawning instance isn't deleted, just temporarily displaced OOB
        if instance.respawn_time:
            instance.y = OOB
            self.server.add_deferred(self.server.respawn_instance, instance.respawn_time * self.server.tickrate, False, instance.pk)
        else:
            self.server.instances.pop(instance.pk)
            instance.delete()

    def grab_item_here(self):
        # Check if we're standing on an item
        for i in self.visible_instances:
            if i.entity.typename in ("Item", "Pickaxe", "Axe", "Ore", "Logs") \
                    and i.y == self.player_instance.y and i.x == self.player_instance.x:

                di = models.Item.objects.get(entity=i.entity)
                leftover = self.add_item_to_inventory(di, i.amount)

                if leftover:
                    i.amount = leftover
                else:
                    # remove instanced item from visible instances
                    self.kill_instance(i)
                return

        self.outgoing.append(packet.DenyPacket("There is no item here."))

    def drop_item(self, p: packet.DropItemPacket):
        inv_item = models.InventoryItem.objects.get(id=p.payloads[0].value)

        # create instance and place here
        inst = models.InstancedEntity(entity=inv_item.item.entity,
                                      room=self.player_instance.room, y=self.player_instance.y,
                                      x=self.player_instance.x, amount=inv_item.amount)
        inst.pk = id(inst)      # guarantees unique id for lifetime of inst
        self.server.instances[inst.pk] = inst

        # remove from player inventory
        inv_item.delete()

        # set despawn countdown (2 mins - 120s)
        self.server.add_deferred(self.server.despawn_instance, self.server.tickrate * 120, False, inst.pk)

    def depart_other(self, p: packet.GoodbyePacket):
        ipk: int = p.payloads[0].value
        if ipk not in self.server.instances:
            return

        inst = self.server.instances[ipk]

        if inst in self.visible_instances:
            self.visible_instances.remove(inst)

        if inst.entity.typename == 'Player':
            self.outgoing.append(packet.ServerLogPacket(f"{inst.entity.name} has departed."))

        self.outgoing.append(p)

    def can_gather(self, node: models.ResourceNode) -> bool:
        requirements = {
            'OreNode': 'Pickaxe',
            'TreeNode': 'Axe'
        }

        cis = models.InventoryItem.objects.filter(player=self.player_info,
                                                  item__entity__typename=requirements[node.entity.typename])
        if not cis:
            self.outgoing.append(packet.ServerLogPacket(f"You do not have a {requirements[node.entity.typename]}."))
            return False

        return True

    def start_gather(self, instance: models.InstancedEntity):
        node = models.ResourceNode.objects.get(entity=instance.entity)

        # check if player has required level and item (e.g. pickaxe)
        if not self.can_gather(node):
            return

        if node.entity.typename == "OreNode":
            self.outgoing.append(packet.ServerLogPacket("You begin to mine at the rocks."))
        elif node.entity.typename == "TreeNode":
            self.outgoing.append(packet.ServerLogPacket("You begin to chop at the tree."))

        if self.actionloop:
            self.server.remove_deferred(self.actionloop)
            self.actionloop = None
        self.actionloop = self.server.add_deferred(self.attempt_gather, self.server.tickrate, True, instance, node)

    def attempt_gather(self, instance: models.InstancedEntity, node: models.ResourceNode):
        # if node has already been killed (by other player)
        if instance.y == OOB:
            if self.actionloop:
                self.server.remove_deferred(self.actionloop)
                self.actionloop = None
            return

        if not self.can_gather(node):
            return

        # change change based on difficulty
        if random.randint(0, 5) == 0:
            # success
            if self.actionloop:
                self.server.remove_deferred(self.actionloop)
                self.actionloop = None

            dropitems = set(models.DropTableItem.objects.filter(droptable=node.droptable))
            for itm in dropitems:
                if random.randint(1, itm.chance) == 1:
                    amt = random.randint(itm.min_amt, itm.max_amt)
                    item = itm.item
                    self.add_item_to_inventory(item, amt)
                    self.outgoing.append(packet.ServerLogPacket(f"You acquire {amt} {item.entity.name}."))

            self.kill_instance(instance)
        else:
            # fail
            self.outgoing.append(packet.ServerLogPacket(f"You continue gathering."))
            pass
        pass

    def move(self, p: packet.MovePacket):
        """
        Updates this protocol's player's position and sends the player back to all
        clients connected to the server.
        """

        if self.actionloop:
            self.server.remove_deferred(self.actionloop)
            self.actionloop = None

        # Calculate the desired destination
        desired_y = self.player_instance.y
        desired_x = self.player_instance.x

        if isinstance(p, packet.MoveUpPacket):
            desired_y -= 1
        elif isinstance(p, packet.MoveRightPacket):
            desired_x += 1
        elif isinstance(p, packet.MoveDownPacket):
            desired_y += 1
        elif isinstance(p, packet.MoveLeftPacket):
            desired_x -= 1

        # Check if we're going to land on a portal
        for instance in self.visible_instances:
            if instance.entity.typename == "Portal" and instance.y == desired_y and instance.x == desired_x:
                portal = models.Portal.objects.get(entity=instance.entity)
                desired_y = portal.linkedy
                desired_x = portal.linkedx
                self.player_instance.y = desired_y
                self.player_instance.x = desired_x
                if self.player_instance.room != portal.linkedroom:
                    self.move_rooms(portal.linkedroom.id)
                    return

            elif instance.entity.typename in ("OreNode", "TreeNode") and instance.y == desired_y and instance.x == desired_x:
                self.start_gather(instance)
                return

        if (0 <= desired_y < self.roommap.height and 0 <= desired_x < self.roommap.width) and (self.roommap.at('solid', desired_y, desired_x) == maps.NOTHING):
            self.player_instance.y = desired_y
            self.player_instance.x = desired_x

            for proto in self.server.protocols_in_room(self.player_instance.room_id):
                proto.process_visible_instances()
        else:
            self.outgoing.append(packet.DenyPacket("Can't move there"))

    def move_rooms(self, dest_roomid: Optional[int]):
        print(f"\nmove_rooms(dest_roomid={dest_roomid})\n")

        if self.logged_in:
            # Tell people in the current (old) room we are leaving
            self.broadcast(packet.GoodbyePacket(self.player_instance.pk))

            # Reset visible entities (so things don't "follow" us between rooms)
            self.visible_instances = set()

        self.logged_in = True

        # Tell our client we're ready to switch rooms so it can reinitialise itself and wait for data again.
        self.outgoing.append(packet.MoveRoomsPacket(dest_roomid))

        # Move db instance to the new room
        self.player_instance.room_id = dest_roomid

        room = self.player_instance.room
        self.roommap = maps.Room(room.pk, room.name, room.file_name)

        self.outgoing.append(packet.OkPacket())
        self.establish_player_in_room()

    def establish_player_in_room(self):
        self.outgoing.append(packet.ServerModelPacket('Room', model_to_dict(self.player_instance.room)))
        self.outgoing.append(packet.ServerModelPacket('Instance', create_dict('Instance', self.player_instance)))

        playerdict = model_to_dict(self.player_info)
        playerdict["entity"] = model_to_dict(self.player_info.entity)
        self.outgoing.append(packet.ServerModelPacket('Player', playerdict))

        self.outgoing.append(packet.WeatherChangePacket(self.server.weather))

        # send inventory to player
        if self.state == self.GET_ENTRY:    # Only send on initial login
            items = models.InventoryItem.objects.filter(player=self.player_info)
            for ci in items:
                self.outgoing.append(packet.ServerModelPacket('InventoryItem', create_dict('InventoryItem', ci)))

        self.state = self.PLAY
        self.broadcast(packet.ServerLogPacket(f"{self.username} has arrived."))

        # Tell other players in view that we have arrived
        for proto in self.server.protocols_in_room(self.player_instance.room_id):
            proto.process_visible_instances()

    def process_visible_instances(self):
        """
        Say goodbye to old entities no longer in view and process the new and still-existing entities in view
        """
        prev_in_view = self.visible_instances

        instances_in_view = set()
        for key, instance in self.server.instances_in_room(self.player_instance.room_id).items():
            if self.coord_in_view(instance.y, instance.x):

                # We don't need to process ourselves. This is done on a less frequent server tick
                # See mlserver.py sync_player_instances
                if instance != self.player_instance:
                    instances_in_view.add(instance)

        # removing logged out players from view
        for instance in list(instances_in_view):  # Convert to list to avoid "Set changed size during iteration"
            if instance.entity.typename == 'Player':
                proto = self.server.get_proto_by_id(instance.entity.pk)
                if not proto or not proto.logged_in:
                    instances_in_view.remove(instance)

        self.visible_instances = instances_in_view

        # Say goodbye to the instances which are no longer in our view
        just_left_view: Set[models.InstancedEntity] = prev_in_view.difference(self.visible_instances)
        for instance in just_left_view:
            self.outgoing.append(packet.GoodbyePacket(instance.pk))

        # Send models for all instances brand new to the view
        new_to_view: Set[models.InstancedEntity] = self.visible_instances.difference(prev_in_view)
        for instance in new_to_view:
            self.outgoing.append(packet.ServerModelPacket('Instance', create_dict('Instance', instance)))

        # Now send deltas for instances which were already in the view but have changed in some way
        already_in_view: Set[models.InstancedEntity] = self.visible_instances.intersection(prev_in_view)
        for current_inst in already_in_view:
            c_inst_dict = create_dict('Instance', current_inst)

            # p_inst_dict = {}
            # for prev_inst in prev_in_view:
            #     if prev_inst.id == current_inst.id:
            #         p_inst_dict = create_dict('Instance', prev_inst)
            #
            # self.debug(f"Old: {p_inst_dict}")
            # self.debug(f"New: {c_inst_dict}")
            # delta_dict = get_dict_delta(p_inst_dict, c_inst_dict)
            # # self.debug(str(delta_dict))
            # if len(delta_dict) > 1: # If more than just the IDs differ
            if True:    # TODO: The above delta check isn't working for some reason...
                self.outgoing.append(packet.ServerModelPacket('Instance', c_inst_dict))

    def tick(self):
        if self.next_packet:
            self.process_packet(self.next_packet)
            self.next_packet = None

        # send all packets in queue back to client in order
        for p in list(self.outgoing):
            self.send_packet(p)
            self.outgoing.popleft()

    def sync_player_instance(self):
        self.outgoing.append(packet.ServerModelPacket('Instance', create_dict('Instance', self.player_instance)))

    def send_packet(self, p: packet.Packet):
        """
        Sends a packet to this protocol's client.
        Call this to communicate information back to the game client application.
        """
        message: bytes = p.tobytes()
        try:
            message = cryptography.encrypt(message, self.client_pub_key)
        except Exception as e:
            self.debug(f"FATAL: Couldn't encrypt packet {p} for sending. Error was {e}. Returning.")
            return
        self.sendString(message)
        self.debug(f"Sent data to my client: {p.tobytes()}")

    def broadcast(self, p: packet.Packet, include_self=False):
        excluding = []
        if not include_self:
            excluding.append(self)
        self.server.broadcast_to_room(p, self.player_instance.room.pk, excluding=excluding)

    def debug(self, message: str):
        print(f"[{self.username if self.username else None}]"
              f"[{self.state.__name__}]"
              f"[{self.player_instance.room.name if self.player_instance else None}]: {message}")

    def coord_in_view(self, y: int, x: int) -> bool:
        yview = self.player_instance.y - 10, self.player_instance.y + 10
        xview = self.player_instance.x - 10, self.player_instance.x + 10

        return yview[0] <= y <= yview[1] and xview[0] <= x <= xview[1]
