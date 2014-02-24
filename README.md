aws-management-suite
====================


# Introduction
This is currently very much a work in progress, and there is much that will be cleaned up over time. The goal of this suite is to
abstract many of the common tasks related to managing cloud infrastructure in AWS.

## Current Features
SSH client
* password or private key based login
* support for sudo login (password or passwordless)
* captures stdout, stderr and exit code from command run
EBS Volumes (managed as groups of volumes)
* create volumes
* attach volumes
* create software raid
* assemble software raid
* mount volume/raid
EBS Snapshots (managed as groups of snapshots)
* pre/post snapshot hooks to enable running commands/scripts on target host before and after starting snapshot to ensure consistent point in time snapshot of all volumes in a raid group
* copy snapshot group
* clone snapshot group to new volume group
  * clone latest snapshot of a volume group or host/instance + mount point
Instance Management
* Currently instances need to be added to the hosts table manually, there is a feature planned to add a discovery script so that many of these things can be automatically populated

## Setup and Configuration
### Initial installation
* Copy sample_settings.py to settings.py and edit AWS, SSH and SUDO access credentials
* A MySQL database needs to be setup for tracking state. The following statements assume that the mysql database and the tool are located on the same host:
 * `CREATE DATABASE ams;` -- Create the schema
 * `GRANT ALL PRIVILEGES ON ams.* TO 'ams_user'@'localhost' IDENTIFIED BY 'mypass';` -- (This will create user with username 'ams_user' and password of 'mypass' and give access to the new schema created)
* Edit TRACKING_DB credentials in settings.py with the proper credentials for your MySQL database
* `pip install -r requirements.txt` will install the handful of external dependencies
* Suggested: add the path to ams directory to your path or add symlink to `ams` script to a directory in the system path
* `ams internals database install` will create the current full version of all of the tables

### Upgrading
If you have updated the code base, just run `ams internals database upgrade` to run the update scripts. Upgrade can be run as often as you like, as it will do nothing if the database version matches the code version.



# Management Tool Usage

## General
All of the functionality is through the command line tool `ams`. It has been implemented as a multi-level nested command parser using the argparse module.<br>
If at any point you need help just add `-h` or `--help` flag to the command line and it will list all available sub-commands and options for the current command.<br>
There are still a few legacy command structures that need to be cleaned up, so there may be some minor changes to the syntax to a few of these, but I will attempt to keep these to an absolute minimum.


## Host/Instance
#### `ams host list`
With no options this lists all host entries in the database

Arguments:

      --zone ZONE         Availability zone to filter results by. This is a prefix
                          search so any of the following is valid with increasing
                          specificity: 'us', 'us-west', 'us-west-2', 'us-west-2a'

----

#### `ams host list host [hostname]`
If `hostname` is given then it will match hostname exactly

Arguments:

      --like LIKE         wildcard matches hostname
      --prefix PREFIX     prefix matches hostname
      --zone ZONE         Availability zone to filter results by. This is a prefix
                          search so any of the following is valid with increasing
                          specificity: 'us', 'us-west', 'us-west-2', 'us-west-2a'


----

#### `ams host list instance_id [instance id]`
If `instance id` is given then it will match instance_id exactly

Arguments:

      --like LIKE         wildcard matches instance id
      --prefix PREFIX     prefix matches instance id
      --zone ZONE         Availability zone to filter results by. This is a prefix
                          search so any of the following is valid with increasing
                          specificity: 'us', 'us-west', 'us-west-2', 'us-west-2a'

----

## Volumes
#### `ams volume list`
With no options this lists all volume groups in the database

Arguments:

      --zone ZONE         Availability zone to filter results by. This is a prefix
                          search so any of the following is valid with increasing
                          specificity: 'us', 'us-west', 'us-west-2', 'us-west-2a'

----

#### `ams volume list host [hostname]`
Lists the volume groups for a host or hosts<br>
If `hostname` is given then it will match hostname exactly

Arguments:

      --like LIKE         wildcard matches hostname
      --prefix PREFIX     prefix matches hostname
      --zone ZONE         Availability zone to filter results by. This is a prefix
                          search so any of the following is valid with increasing
                          specificity: 'us', 'us-west', 'us-west-2', 'us-west-2a'


----

#### `ams volume list instance_id [instance_id]`
Lists the volume groups for an instance or instances<br>
If `instance id` is given then it will match instance_id exactly

Arguments:

      --like LIKE         wildcard matches instance id
      --prefix PREFIX     prefix matches instance id
      --zone ZONE         Availability zone to filter results by. This is a prefix
                          search so any of the following is valid with increasing
                          specificity: 'us', 'us-west', 'us-west-2', 'us-west-2a'


----

#### `ams volume create`
Creates a new volume group (single or multiple disk) and attaches to host. Optionally mounts the volume and configures automounting.

Required arguments: (host | instance), numvols, size

Defaults:

 * stripe-block-size: `256`  (256k chunk size recommended for performance of EBS stripes using xfs)
 * raid-level: `0`
 * filesystem: `xfs`  (note: currently due to implementation constrictions filesystem must be one of the types that can be formatted using mkfs.*)
 * iops: `None`
 * mount-point: `None`   (disk will not be mounted and automounting will not be configured if mount-point not provided)
 * no-automount: `false`  (automounting of volumes/raids will be configured in fstab and mdadm.conf by default unless explicitly disabled)


Arguments:

      -i INSTANCE, --instance INSTANCE
                            instance_id of an instance to attach new volume group
      -H HOST, --host HOST  hostname of an instance to attach new volume group
      -n NUMVOLS, --numvols NUMVOLS
                            Number of EBS volumes to create for the new volume
                            group
      -r {0,1,5,10}, --raid-level {0,1,5,10}
                            Set the raid level for new EBS raid
      -b STRIPE_BLOCK_SIZE, --stripe-block-size STRIPE_BLOCK_SIZE
                            Set the stripe block/chunk size for new EBS raid
      -m MOUNT_POINT, --mount-point MOUNT_POINT
                            Set the mount point for volume. Not required, but
                            suggested
      -a, --no-automount    Disable configuring the OS to automatically mount the
                            volume group on reboot
      -f FILESYSTEM, --filesystem FILESYSTEM
                            Filesystem to partition new raid/volume
      -s SIZE, --size SIZE  Per EBS volume size in GiBs
      -p IOPS, --iops IOPS  Per EBS volume provisioned iops

----

#### `ams volume attach (volume_group_id)`
Attaches provided volume_group_id to a host. Optionally mounts the volume and configures automounting.

Required arguments: (host | instance)

Defaults:

 * mount-point: `None`   (disk will not be mounted and automounting will not be configured if mount-point not provided)
 * no-automount: `false`  (automounting of volumes/raids will be configured in fstab and mdadm.conf by default unless explicitly disabled)

Arguments:

      -i INSTANCE, --instance INSTANCE
                            instance_id of an instance to attach new volume group
      -H HOST, --host HOST  hostname of an instance to attach new volume group
      -m MOUNT_POINT, --mount-point MOUNT_POINT
                            Set the mount point for volume. Not required, but
                            suggested
      -a, --no-automount    Disable configuring the OS to automatically mount the
                            volume group on reboot

----

#### `ams volume mount (volume_group_id)`
Mount a volume group on the host that it is currently attached. Supports mounting to a given mount point or the currently defined mount point for the volume group.

Arguments:

      -m MOUNT_POINT, --mount-point MOUNT_POINT
                            Set the mount point for volume. If not provided, will
                            attempt to use currently defined mount point
      -a, --no-automount    Disable configure the OS to automatically mount the
                            volume group on reboot

----

#### `ams volume automount (volume_group_id)`
Configure automounting for the volume_group_id. If mount point is not provided then it will use the currently defined mount point for the volume.
If neither of these exist then it will configure automounting of the volume where it is currently mounted, otherwise it will fail configuring automounting.

Arguments:

      -m MOUNT_POINT, --mount-point MOUNT_POINT
                            Set the mount point for volume. If not provided, will
                            attempt to use currently defined mount point

----

## Snapshots
#### `ams snapshot create volume volume_group_id`
Create a snapshot of a specific volume_group_id.<br>
PRE and POST are commands that will be run before and after the snapshot, and provide a means to ensure that data is in a consistent state before snapshotting and revert back to normal operation after snapshot has begun.

Arguments:

      --pre PRE             command to run on host to prepare for starting EBS
                            snapshot (will not be run if volume group is not
                            attached)
      --post POST           command to run on host after snapshot (will not be run
                            if volume group is not attached)
      -d DESCRIPTION, --description DESCRIPTION
                            description to add to snapshot(s)

----

#### `ams snapshot create host`
Create a snapshot of a specific volume that is on a host<br>
PRE and POST are commands that will be run before and after the snapshot, and provide a means to ensure that data is in a consistent state before snapshotting and revert back to normal operation after snapshot has begun.

Required arguments: (host | instance), mount-point

Arguments:

  -i INSTANCE, --instance INSTANCE
                        instance_id of an instance to snapshot a volume group
  -H HOST, --host HOST  hostname of an instance to snapshot a volume group
  -m MOUNT_POINT, --mount-point MOUNT_POINT
                        mount point of the volume group to snapshot
  --pre PRE             command to run on host to prepare for starting EBS
                        snapshot (will not be run if volume group is not
                        attached)
  --post POST           command to run on host after snapshot (will not be run
                        if volume group is not attached)
  -d DESCRIPTION, --description DESCRIPTION
                        description to add to snapshot(s)

----

#### `ams snapshot clone snapshot (snapshot_group_id)`
Clone a specific snapshot_group_id into a new volume group and optionally attach and mount the new volume.<br>
This will manage copying snapshot to destination region if the destination region is not the same as where the snapshot group is held.<br>

If iops is provided then the volumes in the new volume group will be created with the provided iops, otherwise the iops of the original volume
group for the snapshot will be used. To create the volumes in the new volume group with no iops when the original volume group had iops,
pass in 0 for iops to explicitly disable.

Required arguments: (zone | host | instance)

Arguments:

      -z ZONE, --zone ZONE  Availability zone to create the new volume group in
      -i INSTANCE, --instance INSTANCE
                            instance id to attach the new volume group to
      -H HOST, --host HOST  hostname to attache the new volume group to
      -m MOUNT_POINT, --mount_point MOUNT_POINT
                            directory to mount the new volume group to
      -a, --no-automount    Disable configuring the OS to automatically mount the
                            volume group on reboot
      -p PIOPS, --iops PIOPS
                            Per EBS volume provisioned iops. Set to 0 to
                            explicitly disable provisioned iops. If not provided
                            then the iops of the original volumes will be used.

----

#### `ams snapshot clone latest volume (volume_group_id)`
Clone the latest snapshot for a volume_group_id and optionally attach and mount the new volume.<br>
This will manage copying snapshot to destination region if the destination region is not the same as where the snapshot group is held.<br>

If iops is provided then the volumes in the new volume group will be created with the provided iops, otherwise the iops of the original volume
group for the snapshot will be used. To create the volumes in the new volume group with no iops when the original volume group had iops,
pass in 0 for iops to explicitly disable.

Required arguments: (zone | host | instance)

Arguments:

      -z ZONE, --zone ZONE  Availability zone to create the new volume group in
      -i INSTANCE, --instance INSTANCE
                            instance id to attach the new volume group to
      -H HOST, --host HOST  hostname to attache the new volume group to
      -m MOUNT_POINT, --mount_point MOUNT_POINT
                            directory to mount the new volume group to
      -a, --no-automount    Disable configuring the OS to automatically mount the
                            volume group on reboot
      -p IOPS, --iops IOPS  Per EBS volume provisioned iops. Set to 0 to
                            explicitly disable provisioned iops. If not provided
                            then the iops of the original volumes will be used.


----

#### `ams snapshot clone latest host (hostname) (src_mount_point)`
Clone the latest snapshot for a host + mount-point and optionally attach and mount the new volume.<br>
This will manage copying snapshot to destination region if the destination region is not the same as where the snapshot group is held.<br>

If iops is provided then the volumes in the new volume group will be created with the provided iops, otherwise the iops of the original volume
group for the snapshot will be used. To create the volumes in the new volume group with no iops when the original volume group had iops,
pass in 0 for iops to explicitly disable.

Required arguments: (zone | host | instance)

Arguments:

      -z ZONE, --zone ZONE  Availability zone to create the new volume group in
      -i INSTANCE, --instance INSTANCE
                            instance id to attach the new volume group to
      -H HOST, --host HOST  hostname to attache the new volume group to
      -m MOUNT_POINT, --mount_point MOUNT_POINT
                            directory to mount the new volume group to
      -a, --no-automount    Disable configuring the OS to automatically mount the
                            volume group on reboot
      -p IOPS, --iops IOPS  Per EBS volume provisioned iops. Set to 0 to
                            explicitly disable provisioned iops. If not provided
                            then the iops of the original volumes will be used.

----

#### `ams snapshot clone latest instance (instance_id) (src_mount_point)`
Clone the latest snapshot for an instance + mount-point and optionally attach and mount the new volume.<br>
This will manage copying snapshot to destination region if the destination region is not the same as where the snapshot group is held.<br>

If iops is provided then the volumes in the new volume group will be created with the provided iops, otherwise the iops of the original volume
group for the snapshot will be used. To create the volumes in the new volume group with no iops when the original volume group had iops,
pass in 0 for iops to explicitly disable.

Required arguments: (zone | host | instance)

Arguments:

      -z ZONE, --zone ZONE  Availability zone to create the new volume group in
      -i INSTANCE, --instance INSTANCE
                            instance id to attach the new volume group to
      -H HOST, --host HOST  hostname to attache the new volume group to
      -m MOUNT_POINT, --mount_point MOUNT_POINT
                            directory to mount the new volume group to
      -a, --no-automount    Disable configuring the OS to automatically mount the
                            volume group on reboot
      -p IOPS, --iops IOPS  Per EBS volume provisioned iops. Set to 0 to
                            explicitly disable provisioned iops. If not provided
                            then the iops of the original volumes will be used.


----

#### `ams snapshot schedule`


----

## Internals


