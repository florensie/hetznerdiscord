import os
from pathlib import Path

from discord.ext import commands
from dotenv import load_dotenv
from mcstatus import MinecraftServer

import hcloud as hcl
from hcloud.server_types.domain import ServerType
from hcloud.volumes.domain import Volume
from hcloud.images.domain import Image
from hcloud.actions.domain import ActionFailedException, ActionTimeoutException
from hcloud.servers.client import BoundServer
from hcloud.actions.client import BoundAction
from hcloud.volumes.client import BoundVolume

load_dotenv()
os.chdir(Path(__file__).parent.absolute())

# Discord config
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ROLE = int(os.getenv("BOT_PRIVILEDGED_ROLE"))
PREFIX = os.getenv("BOT_PREFIX")
discord = commands.Bot(PREFIX)

# Hetzner config
HCLOUD_TOKEN = os.getenv("HCLOUD_TOKEN")
SERVER_NAME = os.getenv("SERVER_NAME")
SERVER_TYPE = ServerType(name=os.getenv("SERVER_TYPE"))
SERVER_IMAGE = Image(name=os.getenv("SERVER_IMAGE"))
hcloud = hcl.Client(HCLOUD_TOKEN)


def get_volume() -> Volume:
    return hcloud.volumes.get_by_name(SERVER_NAME)


def requires_role():
    """Short-hand for commands.has_role(ROLE)"""
    return commands.has_role(ROLE)


@discord.event
async def on_command_error(ctx, error: commands.CommandError):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("You are not allowed to use this bot")
    else:
        # Just print out the error message
        await ctx.send(error)


@discord.event
async def on_ready():
    print(f'Bot authenticated as {discord.user}')


async def handle_actions(ctx: commands.Context, actions: [BoundAction]):
    msg = None
    all_success = True

    for action in actions:
        try:
            action.wait_until_finished()
            end_reason = "completed"
        except ActionFailedException:
            end_reason = "failed"
            all_success = False
        except ActionTimeoutException:
            end_reason = "timed out"
            all_success = False

        msg_content = f"Action {end_reason}: `{action.command}`"
        if msg is None:
            msg = await ctx.send(msg_content)
        else:
            await msg.edit(content=msg.content + f'\r\n{msg_content}')

    return all_success


@discord.command()
# @commands.cooldown(0, 5, commands.BucketType.user)
async def status(ctx: commands.Context):
    """Get the status of the server"""
    server = get_volume().server

    if server is not None:
        ip = server.public_net.ipv4.ip
        if server.status == 'running':
            async with ctx.channel.typing():
                try:
                    mc = MinecraftServer(ip)
                    status = mc.status()
                    await ctx.channel.send(f"Minecraft server running at `{ip}`\r\n"
                                           f"{status.description['text']} | "
                                           f"{status.players.online}/{status.players.max} "
                                           f"| {status.version.name}")
                except Exception as e:
                    await ctx.channel.send(f"Server running at `{ip}` but could not read Minecraft status:\r\n`{e}`")
        else:
            await ctx.channel.send(f"Server `{server.status}` at `{ip}`")
    else:
        await ctx.channel.send(f"No server active")


@discord.command()
@commands.max_concurrency(1)
# @commands.cooldown(0, 30, commands.BucketType.user)
@requires_role()
async def start(ctx: commands.Context):
    """Create and initialize a new server with an attached volume"""
    async with ctx.channel.typing():
        try:
            await ctx.channel.send("Creating a new server, this might take a minute")

            volume: Volume = get_volume()
            if volume is None:
                await ctx.channel.send("Error: could not find server volume")
                return
            elif volume.server is not None:
                # If there is already a server, run the status command
                await status(ctx)
                return
            elif volume.status != 'available':
                await ctx.channel.send("Error: volume is not yet available")
                return

            with open('cloud-init.yaml') as user_data:
                resp = hcloud.servers.create(SERVER_NAME, SERVER_TYPE, SERVER_IMAGE, volumes=[volume],
                                             ssh_keys=hcloud.ssh_keys.get_all(), location=volume.location,
                                             user_data=user_data.read(), automount=True)
            actions = [resp.action] + resp.next_actions
            if not await handle_actions(ctx, actions):
                await ctx.channel.send("An action has failed, aborting")
                return

            await ctx.channel.send(f"New server created at `{resp.server.public_net.ipv4.ip}`")
        except Exception as e:
            await ctx.channel.send(f"An error occured while creating a new server:\r\n`{e}`")


@discord.command()
@commands.max_concurrency(1)
# @commands.cooldown(0, 30, commands.BucketType.user)
@requires_role()
async def stop(ctx: commands.Context):
    """Stop and delete the server, keeping only the volume"""
    async with ctx.channel.typing():
        try:
            # noinspection PyTypeChecker
            volume: BoundVolume = get_volume()
            server = volume.server
            if server is None:
                await ctx.channel.send("Cannot stop server because it doesn't exist")
                return
            elif server.status not in [BoundServer.model.STATUS_OFF, BoundServer.model.STATUS_RUNNING]:
                await ctx.channel.send(f"Server is busy ({server.status}).")
                return
            elif server.locked:
                await ctx.channel.send("Cannot stop server because it is locked")
                return

            # TODO: this completes too soon?
            await ctx.channel.send("Shutting down server...")
            if not await handle_actions(ctx, [server.shutdown()]):
                await ctx.channel.send("An action has failed, aborting")
                return

            await ctx.channel.send("Detatching volume...")
            if not await handle_actions(ctx, [volume.detach()]):
                await ctx.channel.send("An action has failed, aborting")
                return

            await ctx.channel.send("Deleting server...")
            if not await handle_actions(ctx, [server.delete()]):
                await ctx.channel.send("An action has failed, aborting")
                return

            await ctx.channel.send("Server deleted")
        except Exception as e:
            await ctx.channel.send(f"An error occured while stopping the server:\r\n`{e}`")
            return

if __name__ == '__main__':
    discord.run(DISCORD_TOKEN)
