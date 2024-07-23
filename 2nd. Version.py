
#!/usr/bin/python3

"""
About: Example of internal Docker container stateful migration with CRIU (https://criu.org/Main_Page)

Topo:  h1     h2     h3
        |      |      |
       s1 -------------
"""


from containernet.net import *
from containernet.node import DockerSta
from containernet.cli import CLI
from containernet.term import makeTerm
from mininet.log import info, setLogLevel
from plot import Plot2D, Plot3D, PlotGraph


#these libraries are imported because of vnfmanager
import os
import shutil
import subprocess
import sys
import time
from shlex import split
import docker
#end of library importation
#this is the code which we want to add to the containernet 


VNFMANGER_MOUNTED_DIR = "/home/zeynab/mininet-wifi/containernet/examples/one"


class DockerContainer(object):

    """Docker container running INSIDE Docker host"""

    def __init__(self, name, dhost, dimage, dins, dcmd=None, **params):
        self.name = name
        self.dhost = dhost
        self.dimage = dimage
        self.dcmd = dcmd if dcmd is not None else "/usr/bin/env sh"
        self.dins = dins

    def get_current_stats(self):
        return self.dins.stats(decode=False, stream=False)

    def get_logs(self):
        """Get logs from this container."""
        return self.dins.logs(timestamps=True).decode("utf-8")

    def terminate(self):
        """Internal container specific cleanup"""
        pass


class VNFManager(object):

    """Manager for VNFs deployed on Mininet hosts (Docker in Docker)

    - To make is simple. It uses docker-py APIs to manage internal containers
      from host system.

    - It should communicate with SDN controller to manage internal containers
      adaptively.

    Ref:
        [1] https://docker-py.readthedocs.io/en/stable/containers.html
    """
    
    
    docker_args = {
        "tty": True,  # -t
        "detach": True,  # -d
        "labels": {"comnetsemu": "dockercontainer"},
        # Required for CRIU checkpoint
        "security_opt": ["seccomp:unconfined"],
        # Shared directory in host OS
        "volumes": {VNFMANGER_MOUNTED_DIR: {'bind': VNFMANGER_MOUNTED_DIR, 'mode': 'rw'}}
    }


    def __init__(self, net,VNFMANGER_MOUNTED_DIR):
        """Init the VNFManager

        :param net (Mininet): The mininet object, used to manage hosts via
        Mininet's API
        """
        self.net = net
        self.dclt = docker.from_env()
        self.VNFMANGER_MOUNTED_DIR = VNFMANGER_MOUNTED_DIR

        self.container_queue = list()
        self.name_container_map = dict()

    def createContainer(self, name, dhost, dimage, dcmd):
        """Create a container without starting it.

        :param name (str): Name of the container
        :param dimage (str): The name of the docker image
        :param dcmd (str): Command to run after the creation
        """
        self.docker_args["name"] = name
        self.docker_args["image"] = dimage
        self.docker_args["command"] = dcmd
        self.docker_args["cgroup_parent"] = "/docker/{}".format(dhost.did)
        self.docker_args["network_mode"] = "container:{}".format(dhost.did)

        ret = self.dclt.containers.create(**self.docker_args)
        return ret

    def waitContainerStart(self, name):
        """Wait for container to start up running"""
        while True:
            try:
                dins = self.dclt.containers.get(name)
            except docker.errors.NotFound:
                print("Failed to get container:%s" % (name))
                time.sleep(0.1)
            else:
                break

        while not dins.attrs["State"]["Running"]:
            time.sleep(0.1)
            dins.reload()  # refresh information in 'attrs'

    def addContainer(self, name, dhost, dimage, dcmd, **params):
        """Create and run a new container inside a Mininet DockerHost

        The manager retries with retry_cnt times to create the container if the
        dhost can not be found via docker-py API, but can be found in the
        Mininet host list. This happens during e.g. updating the CPU limitation
        of a running DockerHost instance.

        :param name (str): Name of the container
        :param dhost (str or Node): The name or instance of the to be deployed DockerHost instance
        :param dimage (str): The name of the docker image
        :param dcmd (str): Command to run after the creation
        """

        if isinstance(dhost, str):
            dhost = self.net.get(dhost)
        if not dhost:
            error(
                "The internal container must be deployed on a running DockerHost instance \n")
            return None

        dins = self.createContainer(name, dhost, dimage, dcmd)
        dins.start()
        self.waitContainerStart(name)
        container = DockerContainer(name, dhost, dimage, dins)
        self.container_queue.append(container)
        self.name_container_map[container.name] = container
        return container

    def removeContainer(self, container):
        """Remove the internal container

        :param container (str or DockerContainer): Internal container object (or
        its name in string)

        :return: Return True/False for success/fail remove.
        """

        if not container:
            return False

        if isinstance(container, str):
            container = self.name_container_map.get(container, None)

        try:
            self.container_queue.remove(container)
        except ValueError:
            error("Container not found, Cannot remove it.\n")
            return False
        else:
            container.dins.remove(force=True)
            del self.name_container_map[container.name]
            return True

    @staticmethod
    def calculate_cpu_percent(stats):
        """Calculate the CPU usage in percent with given stats JSON data.

        :param stats (json):
        """
        cpu_count = len(stats["cpu_stats"]["cpu_usage"]["percpu_usage"])
        cpu_percent = 0.0
        cpu_delta = float(stats["cpu_stats"]["cpu_usage"]["total_usage"]) - \
            float(stats["precpu_stats"]["cpu_usage"]["total_usage"])
        system_delta = float(stats["cpu_stats"]["system_cpu_usage"]) - \
            float(stats["precpu_stats"]["system_cpu_usage"])
        if system_delta > 0.0:
            cpu_percent = cpu_delta / system_delta * 100.0 * cpu_count

        if cpu_percent > 100:
            cpu_percent = 100

        return cpu_percent

    def monResourceStats(self, container, sample_num=3, sample_period=1):
        """Monitor the resource stats of a container within a given time

        :param container (str or DockerContainer): Internal container object (or
        its name)
        :param mon_time (float): Monitoring time in seconds
        """

        if isinstance(container, str):
            container = self.name_container_map.get(container, None)

        if not container:
            return list()

        n = 0
        usages = list()
        while n < sample_num:
            stats = container.get_current_stats()
            mem_stats = stats["memory_stats"]
            mem_usg = mem_stats["usage"] / (1024 ** 2)
            cpu_usg = self.calculate_cpu_percent(stats)
            usages.append((cpu_usg, mem_usg))
            time.sleep(sample_period)
            n += 1

        return usages

    def migrateCRIU(self, h1, c1, h2):
        """Migrate Docker c1 running on the host h1 to host h2

        Docker checkpoint is an experimental command.  To enable experimental
        features on the Docker daemon, edit the /etc/docker/daemon.json and set
        experimental to true.

        :param h1 (str):
        :param c1 (str):
        :param h2 (str):
 
        Ref: https://www.criu.org/Docker
        """
        if isinstance(c1, str):
            c1 = self.name_container_map.get(c1, None)
        if isinstance(h1, str):
            h1 = self.net.get(h1)
            h2 = self.net.get(h2)

        c1_checkpoint_path = os.path.join(
            self.VNFMANGER_MOUNTED_DIR, "{}".format(c1.name))
        # MARK: Docker-py does not provide API for checkpoint and restore,
        # Docker CLI is used with subprocess as a temp workaround.
        subprocess.run(
            split("docker checkpoint create --checkpoint-dir={} {} "
                  "{}".format(self.VNFMANGER_MOUNTED_DIR, c1.name, c1.name)),
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        # TODO: Emulate copying checkpoint directory between h1 and h2
        time.sleep(0.17)

        #debug("Create a new container on {} and restore it with {}\n".format(
         #   h2, c1.name
        #))
        dins = self.createContainer(
            "{}_clone".format(c1.name), h2, c1.dimage, c1.dcmd)
        # BUG: Customized checkpoint dir is not supported in Docker...
        # ISSUE: https://github.com/moby/moby/issues/37344
        # subprocess.run(
        #     split("docker start --checkpoint-dir={} --checkpoint={} {}".format(
        #         VNFMANGER_MOUNTED_DIR, c1.name, dins.name
        #     )),
        #     check=True
        # )
        subprocess.run(
            split("mv {} /var/lib/docker/containers/{}/checkpoints/".format(
                c1_checkpoint_path, dins.id
            )), check=True
        )
        # MARK: Race condition of somewhat happens here... Docker daemon shows a
        # commit error.
        while True:
            try:
                subprocess.run(
                    split("docker start --checkpoint={} {}".format(
                        c1.name, dins.name
                    )), check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except subprocess.CalledProcessError:
                time.sleep(0.05)
            else:
                break

        self.waitContainerStart(dins.name)

        container = DockerContainer(dins.name, h2, c1.dimage, dins)
        self.container_queue.append(container)
        self.name_container_map[container.name] = container
        shutil.rmtree(c1_checkpoint_path, ignore_errors=True)

        return container

    def stop(self):
        #debug("STOP: {} containers in the VNF queue: {}\n".format(
         #   len(self.container_queue),
          #  ", ".join((c.name for c in self.container_queue))
        #))

        # Avoid missing delete internal containers manually before stop
        for c in self.container_queue:
            c.terminate()
            c.dins.remove(force=True)

        self.dclt.close()
        shutil.rmtree(self.VNFMANGER_MOUNTED_DIR)

# end of the code 




def runContainerMigration():

    net = Containernet()

    mgr = VNFManager(net,VNFMANGER_MOUNTED_DIR = "/home/zeynab/mininet-wifi/containernet/examples/one")

    info('*** Adding docker containers\n')
    p3 = {'position': '0.0,450.0,0.0'}
    p4 = {'position': '0.0,450.0,0.0'}
    p5 = {'position': '0.0,450.0,0.0'}
    sta1 = net.addStation('sta1', ip='10.0.0.1', mac='00:02:00:00:00:01',
                          cls=DockerSta, dimage="ubuntu:trusty", cpu_shares=20,**p3)
    #, position='0,500,0'                      
    sta2 = net.addStation('sta2', ip='10.0.0.2', mac='00:02:00:00:00:02',
                          cls=DockerSta, dimage="ubuntu:trusty", cpu_shares=20,**p4)
    #, position='50,500,0'                      
    sta3 = net.addStation('sta3', ip='10.0.0.3', mac='00:02:00:00:00:03',
                          cls=DockerSta, dimage="ubuntu:trusty", cpu_shares=20,**p5)
    #, position='100,500,0'
   
   
    car = net.addStation('car', mac='00:02:00:00:00:03', ip='10.0.0.4')    
    ap1 = net.addAccessPoint('ap1',position='0,550,0')
    c0 = net.addController('c0')

    info('*** Configuring WiFi nodes\n')
    net.configureWifiNodes()
   # net.plotGraph(max_x=100,max_y=100)
    info('*** Starting network\n')
    sta1.coord = ['40.0,30.0,0.0', '31.0,10.0,0.0', '31.0,30.0,0.0']
    ap1.coord = ['40,25,10','50,60,90','80,120,46']
    sta2.coord = ['40.0,40.0,0.0', '55.0,31.0,0.0', '55.0,81.0,0.0']
    sta3.coord = ['40.0,30.0,0.0', '31.0,10.0,0.0', '31.0,30.0,0.0']
    car.coord = ['40.0,40.0,0.0', '55.0,31.0,0.0', '55.0,81.0,0.0']
    
    """plot graph"""
    net.plotGraph(max_x = 1000,max_y = 1000, min_x = 0, min_y = 0)
        
    net.startMobility(time=0, repetitions=1, AC='ssf')
    

    net.build()
    ap1.start([c0])    
    net.start()
    #net.plotGraph(max_x=1000,max_y=1000)

    p1 = {'position': '0.0,450.0,0.0'}
    p2 = {'position': '160.0,450.0,0.0'}
    net.mobility(car, 'start', time=0, **p1)
    net.mobility(car, 'stop', time=50, **p2)
    
    print("*** Deploy a looper container on sta1")
   # mgr.VNFMANGER_MOUNTED_DIR = "/home/zeynab/mininet-wifi/containernet/examples/one"
    looper = mgr.addContainer(
        "looper", "sta1", "ubuntu:trusty", "/bin/sh -c 'i=0; while true; do echo $i; i=$(expr $i + 1); sleep 1; done'")
    time.sleep(10)
    print("*** Logs of the original looper \n" + looper.get_logs())
    
    
    print("*** Migrate the looper from sta1 to sta2.")
    looper_sta2 = mgr.migrateCRIU(sta1, looper, sta2)
    time.sleep(10)
    print(looper_sta2.get_logs())

   # mgr.docker_args["volumes"][VNFMANGER_MOUNTED_DIR]['bind'] = "/home/zeynab/mininet-wifi/containernet/examples/two"
   
    mgr.VNFMANGER_MOUNTED_DIR = "/home/zeynab/mininet-wifi/containernet/examples/two"
    print("*** Migrate the looper from sta2 to sta3.")
    looper_sta3 = mgr.migrateCRIU(sta2, looper_sta2 , sta3)
    time.sleep(10)
    print(looper_sta3.get_logs())
    
    
    net.stopMobility(time = 60)
    info('*** Stopping network\n')
    mgr.removeContainer(looper)
    mgr.removeContainer(looper_sta2)
    mgr.removeContainer(looper_sta3)
    net.stop()
    mgr.stop()


if __name__ == '__main__':
    setLogLevel('info')
    runContainerMigration()
