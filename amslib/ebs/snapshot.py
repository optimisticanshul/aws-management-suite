__author__ = 'dwayn'
import time
import types
import datetime
import re
import argparse

import boto.ec2
from amslib.core.manager import BaseManager
from volume import VolumeManager
from amslib.ssh.sshmanager import SSHManager
from errors import *


class SnapshotSchedule:
    def __init__(self):
        self.schedule_id = None
        self.hostname = None
        self.instance_id = None
        self.mount_point = None
        self.volume_group_id = None
        self.interval_hour = 1
        self.interval_day = 1
        self.interval_week = 1
        self.interval_month = 1
        self.retain_hourly = 24
        self.retain_daily = 14
        self.retain_weekly = 4
        self.retain_monthly = 12
        self.retain_yearly = 3
        self.pre_command = None
        self.post_command = None
        self.description = None


class SnapshotManager(BaseManager):

    def __get_boto_conn(self, region):
        if region not in self.boto_conns:
            self.boto_conns[region] = boto.ec2.connect_to_region(region, aws_access_key_id=self.settings.AWS_ACCESS_KEY, aws_secret_access_key=self.settings.AWS_SECRET_KEY)
        return self.boto_conns[region]


    # pre_command and post_command can be a string containing a command to run using sudo on host, or they can be a callable function
    # if they are callable then these named parameters will be passed in:  hostname, instance_id
    # note that the pre/post commands will not be executed if the volume group is not attached to a host
    def snapshot_volume_group(self, volume_group_id, description=None, pre_command=None, post_command=None, expiry_date=None):
        # lookup volume_group and instance_id (if attached)
        self.db.execute("select "
                          "vg.volume_group_id, "
                          "vg.raid_level, "
                          "vg.stripe_block_size, "
                          "vg.fs_type, "
                          "vg.block_device, "
                          "vg.group_type, "
                          "vg.tags, "
                          "hv.instance_id, "
                          "hv.mount_point, "
                          "v.availability_zone, "
                          "h.host "
                          "from "
                          "volume_groups vg "
                          "left join host_volumes hv on vg.volume_group_id = hv.volume_group_id "
                          "left join hosts h on h.instance_id = hv.instance_id "
                          "left join volumes v on v.volume_group_id=vg.volume_group_id "
                          "where vg.volume_group_id = %s", (volume_group_id, ))
        vgdata = self.db.fetchone()
        if not vgdata:
            raise VolumeGroupNotFound("Volume group {0} not found".format(volume_group_id))

        region = vgdata[9][0:len(vgdata[9]) - 1]
        botoconn = self.__get_boto_conn(region)

        self.db.execute("select volume_id, size, piops, block_device, raid_device_id, tags from volumes where volume_group_id=%s", (volume_group_id, ))
        voldata = self.db.fetchall()
        if not voldata:
            raise VolumeGroupNotFound("Error fetching volume information for volume group {0}".format(volume_group_id))

        # check if volumes are attached
        volume_ids = []
        for v in voldata:
            volume_ids.append(v[0])
        vols = botoconn.get_all_volumes(volume_ids)

        attached = 0
        for v in vols:
            if v.attachment_state() == "attached":
                attached += 1

        if attached and (attached != len(vols)):
            raise VolumeMountError("Volumes in volume_group_id {0} are partially attached, halting snapshot")


        # run precommand
        if attached:
            if isinstance(pre_command, types.FunctionType):
                pre_command(hostname=vgdata[10], instance_id=vgdata[7])
            elif isinstance(pre_command, types.StringType):
                sh = SSHManager()
                sh.connect(hostname=vgdata[10], port=self.settings.SSH_PORT, username=self.settings.SSH_USER, password=self.settings.SSH_PASSWORD, key_filename=self.settings.SSH_KEYFILE)
                stdout, stderr, exit_code = sh.sudo(pre_command, sudo_password=self.settings.SUDO_PASSWORD)
                if int(exit_code) != 0:
                    raise SnapshotError("There was an error running snapshot pre_command\n{0}\n{1}".format(pre_command, stderr))


        # start snapshot on each volume
        snaps = {}
        snapshots = []
        for vol in voldata:
            snap = botoconn.create_snapshot(volume_id=vol[0], description=description)
            snaps[vol[0]] = snap
            snapshotdata = self.get_snapshot_struct(snap.id, vol[1], vol[4], vol[0], region, vol[3], vol[2], None, vol[5], description)
            snapshots.append(snapshotdata)


        # check to see if any of the snaps errored out
        for vid in snaps.keys():
            snaps[vid].update()
            if snaps[vid].status == 'error':
                raise SnapshotCreateError("There was an error creating snapshot {0} for volume_group_id: {1}".format(snaps[vid].id, volume_group_id))

        # store the metadata for the snapshot group
        self.store_snapshot_group(snapshots, volume_group_id, vgdata[3], vgdata[1], vgdata[2], vgdata[4], vgdata[6], expiry_date)

        # run postcommand
        if attached:
            if isinstance(post_command, types.FunctionType):
                post_command(hostname=vgdata[10], instance_id=vgdata[7])
            elif isinstance(post_command, types.StringType):
                sh = SSHManager()
                sh.connect(hostname=vgdata[10], port=self.settings.SSH_PORT, username=self.settings.SSH_USER, password=self.settings.SSH_PASSWORD, key_filename=self.settings.SSH_KEYFILE)
                stdout, stderr, exit_code = sh.sudo(post_command, sudo_password=self.settings.SUDO_PASSWORD)
                if int(exit_code) != 0:
                    raise SnapshotError("There was an error running snapshot post_command\n{0}\n{1}".format(post_command, stderr))

    # copy an entire snapshot group to a region
    def copy_snapshot_group(self, snapshot_group_id, region):
        botoconn = self.__get_boto_conn(region)
        self.db.execute("select "
                          "s.snapshot_id, "
                          "s.volume_id, "
                          "s.size, "
                          "s.piops, "
                          "s.block_device, "
                          "s.raid_device_id, "
                          "s.created_date, "
                          "s.expiry_date, "
                          "s.region, "
                          "s.tags, "
                          "s.description, "
                          "sg.volume_group_id, "
                          "sg.raid_level, "
                          "sg.stripe_block_size, "
                          "sg.fs_type, "
                          "sg.block_device, "
                          "sg.group_type, "
                          "sg.tags "
                          "from snapshot_groups sg "
                          "join snapshots s on s.snapshot_group_id=sg.snapshot_group_id "
                          "where sg.snapshot_group_id=%s", (snapshot_group_id, ))
        sginfo = self.db.fetchall()
        volume_group_id, raid_level, stripe_block_size, fs_type, block_device, group_type, tags = sginfo[0][11:]

        snap_details = {}
        snaps = {}
        for s in sginfo:
            snap_details[s[0]] = s[:11]
            id = botoconn.copy_snapshot(s[8], s[0], "Copy of {} from {}".format(s[0], s[8]))
            snaps[id] = s[0]

        time.sleep(2)
        new_snaps = botoconn.get_all_snapshots(snaps.values())
        snapshots = []
        for snap in new_snaps:
            details = snap_details[snaps[snap.id]]
            snapshotdata = self.get_snapshot_struct(snap.id, details[2], details[5], details[1], region, details[4], details[3], None, details[9], "Copy of {} from {}".format(details[0], details[8]))
            snapshots.append(snapshotdata)

        pending = True
        while pending:
            pending = False
            for snap in new_snaps:
                snap.update()
                if snap.status == 'error':
                    raise SnapshotCreateError("Error creating snapshot {} as a copy of snapshot {}".format(snap.id, snaps[snap.id]))
                if snap.status == 'pending':
                    pending = True

            if not pending:
                break
            else:
                time.sleep(5)

        new_snapshot_group_id = self.store_snapshot_group(snapshots, volume_group_id, fs_type, raid_level, stripe_block_size, block_device, tags, None)

        return new_snapshot_group_id




    # clones a group of snapshots that represent a snapshot group and creates a new volume group
    # TODO find out if growing the volumes will cause issues with the software raid
    def clone_snapshot_group(self, snapshot_group_id, availability_zone, piops=None, instance_id=None, mount_point=None, automount=True):
        original_snapshot_group_id = None
        region = availability_zone[0:len(availability_zone) - 1]
        botoconn = self.__get_boto_conn(region)
        self.db.execute("select "
                          "snapshot_id, "
                          "volume_id, "
                          "size, "
                          "piops, "
                          "raid_device_id, "
                          "region, "
                          "sg.snapshot_group_id, "
                          "volume_group_id, "
                          "raid_level, "
                          "stripe_block_size, "
                          "fs_type, "
                          "group_type, "
                          "sg.block_device, "
                          "s.block_device, "
                          "s.tags,"
                          "sg.tags "
                          "from snapshot_groups sg "
                          "left join snapshots s on s.snapshot_group_id=sg.snapshot_group_id "
                          "where sg.snapshot_group_id=%s", (snapshot_group_id, ))
        snapshot_group = self.db.fetchall()
        if not snapshot_group:
            raise SnapshotNotFound("Snapshot group {} not found".format(snapshot_group_id))

        source_region = snapshot_group[0][5]
        volume_group_id = snapshot_group[0][7]
        raid_level = snapshot_group[0][8]
        stripe_block_size = snapshot_group[0][9]
        fs_type = snapshot_group[0][10]
        group_type = snapshot_group[0][11]

        # if the snapshot group is not in the same region as where the volume group is being created then it needs to be
        # copied and the new snapshot_group_id used
        if region != source_region:
            original_snapshot_group_id = snapshot_group_id
            snapshot_group_id = self.copy_snapshot_group(original_snapshot_group_id, region)

        snapshot_details = {}
        snapshot_ids = []
        for s in snapshot_group:
            snapshot_ids.append(s[0])
            snapshot_details[s[0]] = s

        snapshots = botoconn.get_all_snapshots(snapshot_ids)

        # check for errors and wait if snapshots are still pending
        #TODO need to decide if this should fail on pending snaps or wait...maybe an option to wait?
        ready = False
        while not ready:
            ready = True
            for s in snapshots:
                if s.status == 'error':
                    raise SnapshotError('Snapshot {} is in an error state, unable to clone snapshot group {}.'.format(s.id, snapshot_group_id))
                if s.status == 'pending':
                    ready = False

            if ready:
                break
            else:
                for s in snapshots:
                    s.update()
                time.sleep(5)

        vm = VolumeManager(self.settings)

        vols = []
        volumes = []
        #for x in range(0, num_volumes):
        for s in snapshots:
            vol_type = 'standard'
            iops = None
            if piops is not None:
                if piops > 0:
                    iops = piops
            else:
                iops = snapshot_details[s.id][3]

            if iops:
                vol_type = 'io1'

            vol = s.create_volume(availability_zone, iops=iops, volume_type=vol_type)
            vols.append(vol)

            volumes.append(vm.get_volume_struct(vol.id, availability_zone, snapshot_details[s.id][2], snapshot_details[s.id][4], None, iops, None, snapshot_details[s.id][14]))
        available = False
        print "Waiting on volumes to become available"
        while not available:
            available = True
            for v in vols:
                v.update()
                if v.volume_state() == 'creating':
                    available = False
                elif v.volume_state() == 'error':
                    raise VolumeNotAvailable("Error creating volume {}".format(v.id))
            time.sleep(2)

        # and available software raid device will be picked when the raid is assembled
        volume_group_id = vm.store_volume_group(volumes, fs_type, raid_level, stripe_block_size, None, snapshot_details[s.id][15], snapshot_group_id)

        if instance_id:
            vm.attach_volume_group(instance_id, volume_group_id)
            vm.assemble_raid(instance_id, volume_group_id, False)
            if mount_point:
                vm.mount_volume_group(instance_id, volume_group_id, mount_point, automount)

        return volume_group_id




    def store_snapshot_group(self, snapshots, volume_group_id, filesystem, raid_level=0, stripe_block_size=256, block_device=None, tags=None, expiry_date=None):
        expdate = None
        raid_type = 'raid'
        if len(snapshots) == 1:
            raid_type = 'single'
        self.db.execute("INSERT INTO snapshot_groups(volume_group_id, raid_level, stripe_block_size, fs_type, block_device, group_type, tags) "
                          "VALUES(%s,%s,%s,%s,%s,%s,%s)", (volume_group_id, raid_level, stripe_block_size, filesystem, block_device, raid_type, tags))
        self.dbconn.commit()
        snapshot_group_id = self.db.lastrowid

        print snapshot_group_id

        for x in range(0, len(snapshots)):
            snapshots[x]['snapshot_group_id'] = snapshot_group_id
            print snapshots[x]

            if expiry_date:
                expdate = expiry_date.strftime('%Y-%m-%d %H:%M:%S')
            self.db.execute("INSERT INTO snapshots(snapshot_id, snapshot_group_id, volume_id, size, piops, block_device, raid_device_id, region, tags, expiry_date, description)"
                              "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (snapshots[x]['snapshot_id'],
                                                                           snapshots[x]['snapshot_group_id'],
                                                                           snapshots[x]['volume_id'],
                                                                           snapshots[x]['size'],
                                                                           snapshots[x]['piops'],
                                                                           snapshots[x]['block_device'],
                                                                           snapshots[x]['raid_device_id'],
                                                                           snapshots[x]['region'],
                                                                           snapshots[x]['tags'],
                                                                           expdate,
                                                                           snapshots[x]['description'] ))
            self.dbconn.commit()


        return snapshot_group_id


    def schedule_snapshot(self, schedule):
        if not isinstance(schedule, SnapshotSchedule):
            raise SnapshotScheduleError("SnapshotSchedule object required")
        insert_vars = []
        sql = 'INSERT INTO snapshot_schedules set'
        if schedule.hostname:
            insert_vars.append(schedule.hostname)
            sql += " hostname=%s"
            if schedule.mount_point:
                insert_vars.append(schedule.mount_point)
                sql += ", mount_point=%s"
            else:
                raise SnapshotScheduleError("Expecting mount_point to be set when using hostname to schedule a snapshot")
        elif schedule.instance_id:
            insert_vars.append(schedule.instance_id)
            sql += " instance_id=%s"
            if schedule.mount_point:
                insert_vars.append(schedule.mount_point)
                sql += ", mount_point=%s"
            else:
                raise SnapshotScheduleError("Expecting mount_point to be set when using instance_id to schedule a snapshot")
        elif schedule.volume_group_id:
            insert_vars.append(schedule.volume_group_id)
            sql += " volume_group_id=%s"
        else:
            raise SnapshotScheduleError("A snapshot must be scheduled for an instance_id, hostname or volume_group_id")

        sql += ', interval_hour=%s, interval_day=%s, interval_week=%s, interval_month=%s, retain_hourly=%s, retain_daily=%s, retain_weekly=%s, retain_monthly=%s, retain_yearly=%s, pre_command=%s, post_command=%s, description=%s'
        insert_vars.append(schedule.interval_hour)
        insert_vars.append(schedule.interval_day)
        insert_vars.append(schedule.interval_week)
        insert_vars.append(schedule.interval_month)
        insert_vars.append(schedule.retain_hourly)
        insert_vars.append(schedule.retain_daily)
        insert_vars.append(schedule.retain_weekly)
        insert_vars.append(schedule.retain_monthly)
        insert_vars.append(schedule.retain_yearly)
        insert_vars.append(schedule.pre_command)
        insert_vars.append(schedule.post_command)
        insert_vars.append(schedule.description)

        self.db.execute(sql, insert_vars)
        self.dbconn.commit()
        schedule.schedule_id = self.db.lastrowid
        return schedule

    def delete_snapshot_schedule(self, schedule_id):
        self.db.execute("delete from snapshot_schedules where schedule_id=%s",(schedule_id, ))
        self.dbconn.commit()

    def edit_snapshot_schedule(self, schedule_id, updates):
        sql = "update snapshot_schedules set "
        update_vars = []
        sets = []
        for name in updates.keys():
            if name in ('pre_command', 'post_command', 'description') and updates[name] == "":
                updates[name] = None
            sets.append(str(name) + "=%s")
            update_vars.append(updates[name])

        sql += ', '.join(sets)
        sql += " where schedule_id=%s"
        update_vars.append(schedule_id)
        self.db.execute(sql, update_vars)
        self.dbconn.commit()


    def run_snapshot_schedule(self, schedule_id=None):
        t = datetime.datetime.today()
        sql = "select " \
                  "schedule_id," \
                  "h.host," \
                  "h.instance_id," \
                  "hv.volume_group_id," \
                  "hv.mount_point," \
                  "ss.volume_group_id," \
                  "interval_hour," \
                  "interval_day," \
                  "interval_week," \
                  "interval_month," \
                  "retain_hourly," \
                  "retain_daily," \
                  "retain_weekly," \
                  "retain_monthly," \
                  "retain_yearly," \
                  "pre_command," \
                  "post_command," \
                  "description " \
                  "from snapshot_schedules ss " \
                  "left join hosts h on h.instance_id=ss.instance_id or h.host=ss.hostname " \
                  "left join host_volumes hv on hv.instance_id=h.instance_id and hv.mount_point=ss.mount_point"

        if schedule_id:
            self.db.execute(sql + " where schedule_id=%s", (schedule_id, ))
            schedules = self.db.fetchall()
            if not schedules:
                print "Schedule not found"
                return
        else:
            self.db.execute(sql)
            schedules = self.db.fetchall()
            if not schedules:
                print "No snapshot schedules found"
                return

        for schedule in schedules:
            expiry_date = None

            # hourly snapshot
            if t.hour % schedule[6] == 0:
                expiry_date = t + datetime.timedelta(hours=schedule[6]*schedule[10])

            # daily snapshot
            if t.hour == 0 and t.day % schedule[7] == 0:
                expiry_date = t + datetime.timedelta(days=schedule[7]*schedule[11])

            # weekly snapshot
            if t.hour == 0 and t.weekday() == 0 and t.isocalendar()[1] % schedule[8] == 0:
                expiry_date = t + datetime.timedelta(weeks=schedule[8]*schedule[12])

            # monthly snapshot
            if t.hour == 0 and t.day == 1 and t.month % schedule[9] == 0:
                expiry_date = t + datetime.timedelta(days=schedule[9]*schedule[13]*30)

            # yearly snapshot
            if t.hour == 0 and t.day == 1 and t.month == 1:
                expiry_date = t + datetime.timedelta(days=schedule[14]*365)

            # if the snapshot should be done then an expiry_date should have been set, or if it was a manual snapshot then kick off the snapshot
            if expiry_date or schedule_id:
                if schedule[5]:
                    volume_group_id = schedule[5]
                else:
                    volume_group_id = schedule[3]

                self.snapshot_volume_group(volume_group_id=volume_group_id, description=schedule[17], pre_command=schedule[15], post_command=schedule[16], expiry_date=expiry_date)


    def get_snapshot_struct(self, snapshot_id, size, raid_device_id, volume_id, region, block_device=None, piops=None, snapshot_group_id=None, tags=None, description=None):
        struct = {
            'snapshot_id': snapshot_id,
            'snapshot_group_id': snapshot_group_id,
            'volume_id': volume_id,
            'size': size,
            'piops': piops,
            'block_device': block_device,
            'raid_device_id': raid_device_id,
            'region': region,
            'tags': tags,
            'description': description,
        }

        return struct

    def argument_parser_builder(self, parser):
        ssubparser = parser.add_subparsers(title="action", dest='action')
        # ams snapshot create
        screateparser = ssubparser.add_parser("create", help="Create a snapshot group of a volume group")
        screatesubparser = screateparser.add_subparsers(title="resource", dest='resource')
        # ams snapshot create volume
        screatevolparser = screatesubparser.add_parser("volume", help="create a snapshot of a given volume_group_id")
        screatevolparser.add_argument('volume_group_id', type=int, help="ID of the volume group to snapshot")
        screatevolparser.add_argument("--pre", help="command to run on host to prepare for starting EBS snapshot (will not be run if volume group is not attached)")
        screatevolparser.add_argument("--post", help="command to run on host after snapshot (will not be run if volume group is not attached)")
        screatevolparser.add_argument("-d", "--description", help="description to add to snapshot(s)")
        screatevolparser.set_defaults(func=self.command_snapshot_create_volume)
        # ams snapshot host
        screatehostparser = screatesubparser.add_parser("host", help="create a snapshot of a specific volume group on a host")
        group = screatehostparser.add_mutually_exclusive_group(required=True)
        group.add_argument('-i', '--instance', help="instance_id of an instance to snapshot a volume group")
        group.add_argument('-H', '--host', help="hostname of an instance to snapshot a volume group")
        group = screatehostparser.add_mutually_exclusive_group(required=True)
        group.add_argument('-m', '--mount-point', help="mount point of the volume group to snapshot")
        screatehostparser.add_argument("--pre", help="command to run on host to prepare for starting EBS snapshot (will not be run if volume group is not attached)")
        screatehostparser.add_argument("--post", help="command to run on host after snapshot (will not be run if volume group is not attached)")
        screatehostparser.add_argument("-d", "--description", help="description to add to snapshot(s)")
        screatehostparser.set_defaults(func=self.command_snapshot_create_host)


        # shared arguments for all of the snapshot clone
        cloneargs = argparse.ArgumentParser(add_help=False)
        group = cloneargs.add_mutually_exclusive_group(required=True)
        group.add_argument("-z", "--zone", help="Availability zone to create the new volume group in")
        group.add_argument("-i", "--instance", help="instance id to attach the new volume group to")
        group.add_argument("-H", "--host", help="hostname to attache the new volume group to")
        cloneargs.add_argument("-m", "--mount_point", help="directory to mount the new volume group to")
        cloneargs.add_argument("-a", "--no-automount", help="Disable configuring the OS to automatically mount the volume group on reboot", action='store_true')
        cloneargs.add_argument("-p", "--piops", type=int, help="Per EBS volume provisioned iops. Set to 0 to explicitly disable provisioned iops. If not provided then the iops of the original volumes will be used.")
        # ams snapshot clone
        scloneparser = ssubparser.add_parser("clone", help="Clone a snapshot group into a new volume group")
        sclonesubparser = scloneparser.add_subparsers(title='type', dest='type')
        scloneparser.set_defaults(func=self.command_snapshot_clone)
        # ams snapshot clone snapshot
        sclonesnapparser = sclonesubparser.add_parser("snapshot", help="Clone a specific snapshot group", parents=[cloneargs])
        sclonesnapparser.add_argument('snapshot_group_id', type=int, help="ID of the snapshot group to clone")
        # ams snapshot clone latest
        sclonelatestparser = sclonesubparser.add_parser("latest", help="Clone the latest snapshot of a volume group")
        sclonelatestsubparser = sclonelatestparser.add_subparsers(title='sub-type', dest='subtype')
        # ams snapshot clone latest volume
        sclonelatestvolumeparser = sclonelatestsubparser.add_parser("volume", help="Clone the latest snapshot for a volume group", parents=[cloneargs])
        sclonelatestvolumeparser.add_argument('volume_group_id', type=int, help="ID of the volume group to clone")
        # ams snapshot clone latest host
        sclonelatesthostparser = sclonelatestsubparser.add_parser("host", help="Clone the latest snapshot for a host & mount point", parents=[cloneargs])
        sclonelatesthostparser.add_argument('hostname', type=int, help="hostname with the volume group to clone")
        sclonelatesthostparser.add_argument("src_mount_point", type=int, help="mount point of the volume group to clone")
        # ams snapshot clone latest instance
        sclonelatestinstanceparser = sclonelatestsubparser.add_parser("instance", help="Clone the latest snapshot for an instance_id & mount point", parents=[cloneargs])
        sclonelatestinstanceparser.add_argument('instance_id', type=int, help="instance_id of the host with the volume group to clone")
        sclonelatestinstanceparser.add_argument("src_mount_point", type=int, help="mount point of the volume group to clone")


        # ams snapshot schedule
        sscheduleparser = ssubparser.add_parser("schedule", help="View, add or edit snapshot schedules")
        sschedulesubparser = sscheduleparser.add_subparsers(title="subaction", dest='subaction')
        # ams snapshot schedule list
        sschedulelistparser = sschedulesubparser.add_parser("list", help="List snapshot schedules")
        sschedulelistparser.add_argument('resource', nargs='?', help="host, instance, or volume", choices=['host', 'volume', 'instance'])
        sschedulelistparser.add_argument('resource_id', nargs='?', help="hostname, instance_id or volume_group_id")
        sschedulelistparser.add_argument("--like", help="search string to use when listing resources")
        sschedulelistparser.add_argument("--prefix", help="search string prefix to use when listing resources")
        sschedulelistparser.set_defaults(func=self.command_snapshot_schedule_list)
        # ams snapshot schedule add/edit shared args
        scheduleaddshared = argparse.ArgumentParser(add_help=False)
        scheduleaddshared.add_argument('-i', '--intervals', type=int, nargs=4, help='Set all intervals at once', metavar=('HOUR', 'DAY', 'WEEK', 'MONTH'))
        scheduleaddshared.add_argument('-r', '--retentions', type=int, nargs=5, help='Set all retentions at once', metavar=('HOURS', 'DAYS', 'WEEKS', 'MONTHS', 'YEARS'))
        scheduleaddshared.add_argument('--int_hour', dest="interval_hour", type=int, help="hourly interval for snapshots", metavar="HOURS")
        scheduleaddshared.add_argument('--int_day', dest="interval_day", type=int, help="daily interval for snapshots", metavar="DAYS")
        scheduleaddshared.add_argument('--int_week', dest="interval_week", type=int, help="weekly interval for snapshots", metavar="WEEKS")
        scheduleaddshared.add_argument('--int_month', dest="interval_month", type=int, help="monthly interval for snapshots", metavar="MONTHS")
        scheduleaddshared.add_argument('--ret_hour', dest="retain_hourly", type=int, help="number of hourly snapshots to keep", metavar="HOURS")
        scheduleaddshared.add_argument('--ret_day', dest="retain_daily", type=int, help="number of daily snapshots to keep", metavar="DAYS")
        scheduleaddshared.add_argument('--ret_week', dest="retain_weekly", type=int, help="number of weekly snapshots to keep", metavar="WEEKS")
        scheduleaddshared.add_argument('--ret_month', dest="retain_monthly", type=int, help="number of monthly snapshots to keep", metavar="MONTHS")
        scheduleaddshared.add_argument('--ret_year', dest="retain_yearly", type=int, help="number of yearly snapshots to keep", metavar="YEARS")
        scheduleaddshared.add_argument("--pre", dest="pre_command", help="command to run on host to prepare for starting EBS snapshot (will not be run if volume group is not attached)")
        scheduleaddshared.add_argument("--post", dest="post_command", help="command to run on host after snapshot (will not be run if volume group is not attached)")
        scheduleaddshared.add_argument('-d', "--description", help="description to add to snapshot")
        # ams snapshot schedule add
        sscheduleaddparser = sschedulesubparser.add_parser("add", help="Create a new snapshot schedule")
        sscheduleaddparser.set_defaults(func=self.command_snapshot_schedule_add)
        sscheduleaddsubparser = sscheduleaddparser.add_subparsers(title="resource", dest="resource")
        # ams snapshot schedule add host
        sscheduleaddhostparser = sscheduleaddsubparser.add_parser("host", help="add a snapshot to the schedule for a specific hostname (recommended)", parents=[scheduleaddshared])
        sscheduleaddhostparser.add_argument("hostname", help="hostname to schedule snapshots for")
        group = sscheduleaddhostparser.add_mutually_exclusive_group(required=True)
        group.add_argument('-m', '--mount-point', help="mount point of the volume group to snapshot")
        # ams snapshot schedule add instance
        sscheduleaddinstparser = sscheduleaddsubparser.add_parser("instance", help="add a snapshot to the schedule for a specific instance_id", parents=[scheduleaddshared])
        sscheduleaddinstparser.add_argument("instance_id", help="instance_id to schedule snapshots for")
        group = sscheduleaddinstparser.add_mutually_exclusive_group(required=True)
        group.add_argument('-m', '--mount-point', help="mount point of the volume group to snapshot")
        # ams snapshot schedule add volume
        sscheduleaddinstparser = sscheduleaddsubparser.add_parser("volume", help="add a snapshot to the schedule for a specific volume_group_id", parents=[scheduleaddshared])
        sscheduleaddinstparser.add_argument("volume_group_id", help="volume_group_id to schedule snapshots for")
        # ams snapshot schedule edit
        sscheduleeditparser = sschedulesubparser.add_parser("edit", help="Edit a snapshot schedule. hostname, instance_id, volume_group_id, and mount_point cannot be edited", parents=[scheduleaddshared])
        sscheduleeditparser.add_argument('schedule_id', type=int, help="Snapshot schedule_id to edit (use 'ams snapshot schedule list' to list available schedules)")
        sscheduleeditparser.set_defaults(func=self.command_snapshot_schedule_edit)
        # ams snapshot schedule delete
        sscheduledelparser = sschedulesubparser.add_parser("delete", help="Delete a snapshot schedule", parents=[scheduleaddshared])
        sscheduledelparser.add_argument('schedule_id', type=int, help="Snapshot schedule_id to delete (use 'ams snapshot schedule list' to list available schedules)")
        sscheduledelparser.set_defaults(func=self.command_snapshot_schedule_delete)
        # ams snapshot schedule run
        sschedulerunparser = sschedulesubparser.add_parser("run", help="Run the scheduled snapshots now")
        sschedulerunparser.add_argument('schedule_id', nargs='?', type=int, help="Snapshot schedule_id to run. If not supplied, then whatever is scheduled for the current time will run")
        sschedulerunparser.set_defaults(func=self.command_snapshot_schedule_run)


    def command_snapshot_clone(self, args):
        zone = None
        mount_point = None
        automount = True
        instance_id = None
        if args.host:
            self.db.execute("select availability_zone, instance_id from hosts where host=%s", (args.host, ))
            r = self.db.fetchone()
            if not r:
                print "Host '{}' not found".format(args.host)
                return
            zone, instance_id = r
            if args.no_automount:
                automount = False
            mount_point = args.mount_point
        elif args.instance:
            self.db.execute("select availability_zone from hosts where instance_id=%s", (args.instance, ))
            r = self.db.fetchone()
            if not r:
                print "Instance '{}' not found".format(args.instance)
                return
            zone = r[0]
            if args.no_automount:
                automount = False
            mount_point = args.mount_point
            instance_id = args.instance
        elif args.zone:
            zone = args.zone

        snapshot_group_id = None
        if args.type == 'snapshot':
            snapshot_group_id = args.snapshot_group_id
            pass
        elif args.type == 'latest':
            if args.subtype == "volume":
                self.db.execute("select snapshot_group_id from snapshot_groups where volume_group_id=%s order by snapshot_group_id desc limit 1", (args.volume_group_id, ))
                r = self.db.fetchone()
                if not r:
                    print "No snapshots found for volume group {}".format(args.volume_group_id)
                    return
                snapshot_group_id = r[0]
            elif args.subtype == "host":
                self.db.execute("select volume_group_id from hosts h join host_volumes hv on h.instance_id = hv.instance_id where host=%s and mount_point=%s", (args.host, args.src_mount_point))
                r = self.db.fetchone()
                if not r:
                    print "Volume group not found for {} on {}".format(args.src_mount_point, args.host)
                    return
                self.db.execute("select snapshot_group_id from snapshot_groups where volume_group_id=%s order by snapshot_group_id desc limit 1", (r[0], ))
                r = self.db.fetchone()
                if not r:
                    print "No snapshots found for {} on {}".format(args.src_mount_point, args.host)
                    return
                snapshot_group_id = r[0]

            elif args.subtype == "instance":
                self.db.execute("select volume_group_id from hosts h join host_volumes hv on h.instance_id = hv.instance_id where h.instance_id=%s and mount_point=%s", (args.instance, args.src_mount_point))
                r = self.db.fetchone()
                if not r:
                    print "Volume group not found for {} on {}".format(args.src_mount_point, args.instance)
                    return
                self.db.execute("select snapshot_group_id from snapshot_groups where volume_group_id=%s order by snapshot_group_id desc limit 1", (r[0], ))
                r = self.db.fetchone()
                if not r:
                    print "No snapshots found for {} on {}".format(args.src_mount_point, args.instance)
                    return
                snapshot_group_id = r[0]

        volume_group_id = self.clone_snapshot_group(snapshot_group_id, zone, args.piops, instance_id, mount_point, automount)
        print "Volume group {} created".format(volume_group_id)

    def command_snapshot_create_volume(self, args):
        self.snapshot_volume_group(args.volume_group_id, args.description, args.pre, args.post)


    def command_snapshot_create_host(self, args):
        whereclauses = []
        if args.instance:
            whereclauses.append("h.instance_id = '{}'".format(args.instance))
        elif args.host:
            whereclauses.append("h.host = '{}'".format(args.host))

        if args.mount_point:
            whereclauses.append("hv.mount_point = '{}'".format(args.mount_point))

        sql = "select " \
              "hv.volume_group_id " \
              "from " \
              "hosts h " \
              "left join host_volumes hv on h.instance_id=hv.instance_id "
        sql += " where " + " and ".join(whereclauses)
        self.db.execute(sql)
        res = self.db.fetchone()
        if res:
            self.snapshot_volume_group(res[0], args.description, args.pre, args.post)
        else:
            print "Volume group not found"
            exit(1)

    def command_snapshot_schedule_list(self, args):
        whereclauses = []
        order_by = ''
        if args.resource == 'host':
            if args.resource_id:
                whereclauses.append("hostname = '{}'".format(args.resource_id))
            elif args.prefix:
                whereclauses.append("hostname like '{}%'".format(args.prefix))
            elif args.like:
                whereclauses.append("hostname like '%{}%'".format(args.like))
            order_by = " order by hostname asc"
        elif args.resource == 'instance':
            if args.resource_id:
                whereclauses.append("instance_id = '{}'".format(args.resource_id))
            elif args.prefix:
                whereclauses.append("instance_id like '{}%'".format(args.prefix))
            elif args.like:
                whereclauses.append("instance_id like '%{}%'".format(args.like))
            order_by = " order by instance_id asc"
        elif args.resource == 'volume':
            if args.resource_id:
                whereclauses.append("volume_group_id = {}".format(args.resource_id))

        sql = "select " \
              "schedule_id," \
              "hostname," \
              "instance_id," \
              "mount_point," \
              "volume_group_id," \
              "concat(interval_hour,'-',interval_day,'-',interval_week,'-',interval_month)," \
              "concat(retain_hourly,'-',retain_daily,'-',retain_weekly,'-',retain_monthly,'-',retain_yearly)," \
              "pre_command," \
              "post_command," \
              "description " \
              "from snapshot_schedules "
        if whereclauses:
            sql += " and ".join(whereclauses)
        sql += order_by
        self.db.execute(sql)
        results = self.db.fetchall()
        if self.settings.human_output:
            print "Snapshot Schedules:"
            print "schedule_id\thostname\tinstance_id\tmount_point\tvolume_group_id\tintervals(h-d-w-m)\tretentions(h-d-w-m-y)\tpre_command\tpost_command\tdescription"
            print "---------------------------------------------------------------------------------------------------------------------------------------------------"
        for res in results:
            print "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}".format(res[0],res[1],res[2],res[3],res[4],res[5],res[6],res[7],res[8],res[9])
        if self.settings.human_output:
            print "---------------------------------------------------------------------------------------------------------------------------------------------------"


    def command_snapshot_schedule_add(self, args):
        schedule = SnapshotSchedule()
        processargs = [
            'interval_hour',
            'interval_day',
            'interval_week',
            'interval_month',
            'retain_hourly',
            'retain_daily',
            'retain_weekly',
            'retain_monthly',
            'retain_yearly',
            'description',
            'pre_command',
            'post_command',
        ]

        if args.resource == 'host':
            schedule.hostname = args.host
        if args.resource == 'instance':
            schedule.instance_id = args.instance_id
        if args.resource in ('host', 'instance'):
            schedule.mount_point = args.mount_point
        if args.resource == 'volume':
            schedule.volume_group_id = args.volume_group_id

        for arg in processargs:
            if getattr(args, arg):
                setattr(schedule, arg, getattr(args, arg))

        if args.intervals:
            schedule.interval_hour = args.intervals[0]
            schedule.interval_day = args.intervals[1]
            schedule.interval_week = args.intervals[2]
            schedule.interval_month = args.intervals[3]
        if args.retentions:
            schedule.retain_hourly = args.retentions[0]
            schedule.retain_daily = args.retentions[1]
            schedule.retain_weekly = args.retentions[2]
            schedule.retain_monthly = args.retentions[3]
            schedule.retain_yearly = args.retentions[4]

        self.schedule_snapshot(schedule)

    def command_snapshot_schedule_edit(self, args):
        processargs = [
            'interval_hour',
            'interval_day',
            'interval_week',
            'interval_month',
            'retain_hourly',
            'retain_daily',
            'retain_weekly',
            'retain_monthly',
            'retain_yearly',
            'description',
            'pre_command',
            'post_command',
        ]
        updates = {}

        for arg in processargs:
            if getattr(args, arg) is not None:
                updates[arg] = getattr(args, arg)

        if args.intervals:
            updates['interval_hour'] = args.intervals[0]
            updates['interval_day'] = args.intervals[1]
            updates['interval_week'] = args.intervals[2]
            updates['interval_month'] = args.intervals[3]
        if args.retentions:
            updates['retain_hourly'] = args.retentions[0]
            updates['retain_daily'] = args.retentions[1]
            updates['retain_weekly'] = args.retentions[2]
            updates['retain_monthly'] = args.retentions[3]
            updates['retain_yearly'] = args.retentions[4]

        if not len(updates):
            print "Must provide something to update on a snapshot schedule"
            return

        self.edit_snapshot_schedule(args.schedule_id, updates)


    def command_snapshot_schedule_delete(self, args):
        self.delete_snapshot_schedule(args.schedule_id)


    def command_snapshot_schedule_run(self, args):
        self.run_snapshot_schedule(args.schedule_id)

