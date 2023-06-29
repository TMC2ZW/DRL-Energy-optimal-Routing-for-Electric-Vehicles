import numpy as np
import torch
from torch.utils.data import Dataset
import matplotlib.pyplot as plt
from utils.functions import read_file
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


class VehicleRoutingDataset(Dataset):
    def __init__(self, num_samples, input_size, t_limit, Start_SOC,velocity, max_load, max_demand, charging_num,
                 seed, args):
        super(VehicleRoutingDataset, self).__init__()
        if seed is None:
            seed = np.random.randint(1234567890)
        np.random.seed(seed)
        torch.manual_seed(seed)
        # 图的参数
        if args.CVRP_lib_test:
            num_samples = 1
        self.num_samples = num_samples
        self.max_load = max_load
        self.max_demand = max_demand
        self.Start_SOC = Start_SOC
        self.t_limit = t_limit
        self.velocity = velocity
        self.charging_num = charging_num
        self.input_size = input_size
        # 车辆能耗参数
        self.mc = 4100
        self.g = 9.81
        self.w = 1000
        self.Cd = 0.7
        self.A = 6.66
        self.Ad = 1.2041
        self.Cr = 0.01
        self.motor_d = 1.18
        self.motor_r = 0.85
        self.battery_d = 1.11
        self.battery_r = 0.93
        self.charging_time = 1  # 充电一个小时
        self.serve_time = 0.33  # 顾客处服务20分钟
        # 充电站坐标生成
        coordinate_x1 = torch.randint(0, 50, (num_samples, 1, 20))
        coordinate_x2 = torch.randint(51, 101, (num_samples, 1, 20))
        coordinate_y1 = torch.randint(0, 50, (num_samples, 1, 20))
        coordinate_y2 = torch.randint(51, 101, (num_samples, 1, 20))
        c = self.charging_num - 1 if args.depot_charging else self.charging_num
        stations = torch.zeros(num_samples, 2, c)
        for i in range(c):
            if i % 4 == 0:
                stations[:, :, i] = torch.cat((coordinate_x1[:,:,i], coordinate_y1[:,:,i]), dim=1)
            if i % 4 == 1:
                stations[:, :, i] = torch.cat((coordinate_x1[:,:,i], coordinate_y2[:,:,i]), dim=1)
            if i % 4 == 2:
                stations[:, :, i] = torch.cat((coordinate_x2[:,:,i], coordinate_y1[:,:,i]), dim=1)
            if i % 4 == 3:
                stations[:, :, i] = torch.cat((coordinate_x2[:,:,i], coordinate_y2[:,:,i]), dim=1)
        dynamic_shape = (num_samples, 1, input_size + 1 + charging_num)
        if args.CVRP_lib_test:
            all_loc, all_demand, capacity= read_file(args.CVRP_lib_path)
            depot = torch.tensor(all_loc[0])[None, :, None]
            locations = torch.tensor(all_loc[1:])[None, :, :].permute(0, 2, 1)
            locations = torch.cat([depot, stations, locations], dim = 2)
            cus_demands = torch.tensor(all_demand[1:])[None, None, :]
            cus_demands = cus_demands / float(capacity)
            station_demands = torch.zeros((1, 1, 1 + self.charging_num))
            demands = torch.cat([station_demands, cus_demands], dim=2)
        else:
            depot = torch.randint(25, 75, (num_samples, 2, 1))
            depot_charging = depot
            locations = torch.randint(0, 101, (num_samples, 2, input_size))
            locations = torch.cat((depot, depot_charging,  stations, locations),2)
            demands = torch.randint(1, max_demand + 1, dynamic_shape) * 0.25
            demands = demands / float(max_load)
            demands[:, :, 0:1 + charging_num] = 0
        self.static = locations
        # 高程的生成，单位 m
        Elevations =torch.randint(0,101,(num_samples, input_size + 1 + charging_num),device=device)
        Elevations =( Elevations / 1000 )
        loads = torch.full(dynamic_shape, 1.)
        self.Elevation = Elevations                                                            # 充电站和仓库的需求设置为0
        SOC = torch.full(dynamic_shape, self.Start_SOC)                                        # 电池容量
        time1 = torch.full(dynamic_shape,t_limit)                                              # 设置时间限制
        self.dynamic = torch.as_tensor(np.concatenate((loads, demands, SOC, time1), axis=1))

        if (input_size <= 20 or num_samples <= 12800):  # 问题规模较小是则在制作数据时直接计算
            seq_len = 1 + charging_num + input_size
            self.distances = torch.zeros(num_samples, seq_len, seq_len,device=device,)                          # 计算距离矩阵
            for i in range(seq_len):
                self.distances[:, i] = torch.sqrt(torch.sum(torch.pow(self.static[:,:,i:i+1]-self.static[:,:,:],2),dim=1))
            self.slope =torch.zeros(num_samples, seq_len, seq_len,device=device)
            for i in range(seq_len):
                self.slope[:, i] = torch.clamp(torch.div((Elevations[:,i:i+1]-Elevations[:,:]),self.distances[:, i]+0.000001),min=-0.10,max=0.10)

    def __len__(self):
        return self.num_samples

    def __getitem__(self, idx):
        # (static, dynamic, distances) 问题规模大了会爆显存
        if (self.input_size <= 20 or self.num_samples <= 12800):

            return (self.static[idx], self.dynamic[idx],self.distances[idx],self.slope[idx])

        else:
            return (self.static[idx], self.dynamic[idx], self.Elevation[idx])


    def update_mask(self, dynamic, distances, slope, chosen_idx=None):
        """
        已经选择点之后不能去的点
        :param dynamic:到达选择点之后，已经更新的动态信息
        :param distances:距离矩阵
        :param slope:坡度矩阵
        :param chosen_idx:当前所在点
        :return:下回合选择点时的屏蔽矩阵
        """
        depot = chosen_idx.eq(0)                                                            # 去仓库的batch
        charging_station = (chosen_idx.gt(0) & chosen_idx.le(self.charging_num))            # 充电站的batch
        station_depot = chosen_idx.le(self.charging_num)                                    # 充电站和仓库的batch
        customer = chosen_idx.gt(self.charging_num)                                         # 选择客户的batch

        loads = dynamic.data[:, 0]                                                          # (batch_size, seq_len)
        demands = dynamic.data[:, 1]
        SOC = dynamic.data[:,2]
        time = dynamic.data[:,3]

        chosen_idx = chosen_idx.type(torch.long)

        # 如果需求为0的话则直接返回，所有的点全部屏蔽，标志着回合结束
        # print(demands[demands.any(dim=1)])
        if demands.eq(0).all():
            # print("*"*200)
            return demands * 0.

        #屏蔽条件1：需求大于0且需求量小于装载量
        new_mask = demands.ne(0) * demands.lt(loads)

        #屏蔽条件2：这一时刻被选择的点将在下一时刻被屏蔽
        new_mask.scatter_(1, chosen_idx.unsqueeze(1), 0)                                    # 这一回合直接屏蔽上一回合选择过的点

        #屏蔽条件3：充电站访问的限制
        new_mask[station_depot, 1:self.charging_num + 1] = 0                                # 选择过的充电站和仓库后，不能再选择充电站了
        new_mask[charging_station, 0] = 1                                                   # 把选择充电站的仓库设置为 1
        new_mask[customer, :self.charging_num + 1] = 1                                      # 选择客户的batch,下一回合可以访问充电站和仓库，充电站和仓库可以多次访问
        new_mask[depot,0]=0                                                                 # 选择完仓库之后不能再选仓库

        # 屏蔽条件4：没有货物量了就直接返回仓库，或者客户没有需求量了
        has_no_load = loads[:, 0].eq(0).float()
        has_no_demand = demands[:, self.charging_num:].sum(1).eq(0).float()
        combined = (has_no_load + has_no_demand).gt(0)
        if combined.any():
            new_mask[combined.nonzero(as_tuple=False), 0] = 1
            new_mask[combined.nonzero(as_tuple=False), 1:] = 0

        # 屏蔽条件5，下次解码电量或时间不能直接到达的点: ->节点->节点
        distance0 = distances[torch.arange(distances.size(0)), chosen_idx].clone()                  #选出t时刻所选择的点到其它点的距离
        slope1 = slope[torch.arange(distances.size(0)),  chosen_idx]
        time_cons0 = distance0 / self.velocity  # t时刻所选择的点到其它点所消耗的时间

        mass0 = (self.mc + loads[:, 0] * self.max_load * self.w).unsqueeze(1).expand_as(distance0)   # (batch,sequence_len)
        Pm0 = (0.5*self.Cd*self.A*self.Ad*(self.velocity/3.6)**2+mass0*self.g*slope1+mass0*self.g*self.Cr)*self.velocity    #行驶功率的确定
        positive_index0 = Pm0.gt(0.)
        negative_index0 = Pm0.lt(0.)
        soc_consume0 = torch.zeros_like(Pm0)
        soc_consume0[positive_index0]=self.motor_d*self.battery_d*Pm0[positive_index0]*time_cons0[positive_index0] / 3600.
        soc_consume0[negative_index0]=self.motor_r*self.battery_r*Pm0[negative_index0]*time_cons0[negative_index0] / 3600.
        SOC_cons0 = soc_consume0                                                                     # t时刻所选择的点到其它点消耗的soc

        # 屏蔽条件6，下次解码因为返回仓库时间或不够的:  -> 节点 -> 仓库
        # 先去一个点的行驶能耗计算
        time_cons1 = distance0 / self.velocity
        slope1 = slope[torch.arange(distances.size(0)), chosen_idx]                               # (batch, sequence_len)
        mass1 = (self.mc + loads[:, 0] * self.max_load * self.w).unsqueeze(1).expand_as(distance0)
        Pm1 = (0.5 * self.Cd * self.A * self.Ad * (self.velocity/3.6) ** 2 + mass1 * self.g * slope1 + mass1 * self.g * self.Cr) * self.velocity  # 行驶功率的确定
        positive_index1 = Pm1.gt(0.)
        negative_index1 = Pm1.lt(0.)
        soc_consume1 = torch.zeros_like(Pm1)
        soc_consume1[positive_index1] = self.motor_d * self.battery_d * Pm1[positive_index1] * time_cons1[positive_index1] / 3600.
        soc_consume1[negative_index1] = self.motor_r * self.battery_r * Pm1[negative_index1] * time_cons1[negative_index1] / 3600.
        SOC_cons1 = soc_consume1
        # 再从这个点返回仓库的能耗
        mass2 = (self.mc + (loads - demands)  * self.max_load * self.w)
        time_cons2 = distances[:, 0, :] / self.velocity
        slope2 =slope[torch.arange(distances.size(0)), 0 ]
        Pm2 = (0.5 * self.Cd * self.A * self.Ad * (self.velocity/3.6) ** 2 + mass2 * self.g * slope2 + mass2 * self.g * self.Cr) * self.velocity  # 行驶功率的确定
        positive_index2 = Pm2.gt(0.)
        negative_index2 = Pm2.lt(0.)
        soc_consume2 = torch.zeros_like(Pm2)
        soc_consume2[positive_index2] = self.motor_d * self.battery_d * Pm2[positive_index2] * time_cons2[positive_index2] / 3600.
        soc_consume2[negative_index2] = self.motor_r * self.battery_r * Pm2[negative_index2] * time_cons2[negative_index2] / 3600.
        SOC_cons2 = soc_consume2

        SOC_cons3 = SOC_cons1 + SOC_cons2
        SOC_cons3[:, 0:self.charging_num + 1 ] = 0
        time_cons3 = (distances[torch.arange(distances.size(0)), chosen_idx] + distances[:, 0, :]) / self.velocity   #去客户点
        time_cons3[:, 1:self.charging_num + 1] += self.charging_time
        time_cons3[:, self.charging_num + 1:] += self.serve_time

        # 屏蔽条件7: 下次解码绕道到最近充电站而因为时间或者电量不能返回仓库的->节点-> 充电站-> 仓库
        distances_station = [distances[:, i:i+1, :] for i in range(1, self.charging_num + 1)]
        distances_station = torch.cat(distances_station, dim=1)  # [batch, charging_num, sequence_num]
        distances_station = torch.min(distances_station, dim=1)  # [batch,  sequence_num] 每个点去一个充电站再返回仓库的最短距离 降维 0为值 1为索引
        distances_station[0][:, 0] = 0
        distance3 = distances_station[0]
        slope3 = slope[:, 1:self.charging_num + 1, :].gather(1, distances_station[1].unsqueeze(1)).squeeze(1)  # 取出最近的一个点的坡度[batch, sequence_len]
        distance4 = distances[:, 1:self.charging_num + 1, 0:1].gather(1, distances_station[1].unsqueeze(1)[:, :,0:1]).squeeze(1)
        time_cons4 = distance3 / self.velocity
        Pm3 = (0.5 * self.Cd * self.A * self.Ad * (self.velocity / 3.6) ** 2 + mass2 * self.g * slope3 + mass2 * self.g * self.Cr) * self.velocity  # 行驶功率的确定
        positive_index2 = Pm3.gt(0.)
        negative_index2 = Pm3.lt(0.)
        soc_consume4 = torch.zeros_like(Pm3)
        # 计算到最近的充电站的能耗
        soc_consume4[positive_index2] = self.motor_d * self.battery_d * Pm3[positive_index2] * time_cons4[positive_index2] / 3600.
        soc_consume4[negative_index2] = self.motor_r * self.battery_r * Pm3[negative_index2] * time_cons4[negative_index2] / 3600.
        SOC_cons4 = soc_consume4
        SOC_cons5 = SOC_cons1 + SOC_cons4  # 去任意一个站点再去最近的充电站的能耗[batch, sequence_len]
        time_cons5 = (distance0 + distance3 + distance4) / self.velocity
        time_cons5[:, self.charging_num + 1:] += (self.charging_time + self.serve_time)
        # 屏蔽
        new_mask[(SOC < SOC_cons0) | (time < time_cons0)] = 0

        # print(SOC_cons3[demands.any(dim=1)])
        # print(SOC_cons5[demands.any(dim=1)])

        new_mask[((SOC < SOC_cons3) | (time < time_cons3)) & ((SOC < SOC_cons5) | (time < time_cons5))] = 0

        all_masked = new_mask[:, self.charging_num + 1:].eq(1).sum(1).le(0)
        new_mask[all_masked, 0] = 1

        return new_mask.float()


    def update_dynamic(self, dynamic, distances, slope, now_idx, chosen_idx):
        """更新动态信息.
        其中电能、装载量、时间是一起更新的，需求量是分开更新的
        """
        now_idx =now_idx.type(torch.int64)
        chosen_idx = chosen_idx.type(torch.int64)

        distance = distances[torch.arange(distances.size(0)), now_idx, chosen_idx].unsqueeze(1)   #获得两点的距离
        slope = slope[torch.arange(distances.size(0)), now_idx, chosen_idx].unsqueeze(1)     #获得两点之间的坡度(batch,1)

        depot = chosen_idx.eq(0)                                                                # 去仓库的batch
        charging_station = (chosen_idx.gt(0) & chosen_idx.le(self.charging_num))                # 充电站的batch
        station_depot = chosen_idx.le(self.charging_num)                                        # 充电站和仓库的batch
        customer = chosen_idx.gt(self.charging_num)                                             # 选择客户的batch

        all_loads = dynamic[:, 0].clone()                                                       # (batch_size, seq_len)
        all_demands = dynamic[:, 1].clone()                                                     # (batch_size, seq_len)
        all_SOC = dynamic[:,2].clone()                                                          # (batch_size, seq_len)
        all_time = dynamic[:,3].clone()                                                         # (batch_size, seq_len)

        time_cons = distance / self.velocity

        mass = (self.mc + all_loads[:,0]*self.max_load*self.w).unsqueeze(1)                            #(batch,1)
        Pm = (0.5*self.Cd*self.A*self.Ad*(self.velocity/3.6)**2+mass*self.g*slope+mass*self.g*self.Cr)*self.velocity    #行驶功率的确定
        positive_index = Pm.gt(0.)
        negative_index = Pm.lt(0.)
        soc_consume = torch.zeros_like(Pm)
        soc_consume[positive_index]=self.motor_d*self.battery_d*Pm[positive_index]*time_cons[positive_index] / 3600.
        soc_consume[negative_index]=self.motor_r*self.battery_r*Pm[negative_index]*time_cons[negative_index] / 3600.

        all_time -= time_cons                                                                    # 总时间减去每个batch路径需要的时间
        all_time[depot] = self.t_limit                                                           # 返回仓库的恢复时间
        all_time[charging_station] -= self.charging_time                                         # 去充电站的减去0.25 充电
        all_time[customer] -= self.serve_time                                                    # 去客户的减去0.5 服务时间
        all_SOC -= soc_consume                                                                   # 更新剩余电量
        all_SOC[station_depot] = self.Start_SOC                                                  # 去充电站的或者仓库的都边满

        load = torch.gather(all_loads, 1, chosen_idx.unsqueeze(1))
        demand = torch.gather(all_demands, 1, chosen_idx.unsqueeze(1))

        if customer.any():
            new_load = torch.clamp(load - demand, min=0)
            new_demand = torch.clamp(demand - load, min=0)

            customer_idx = customer.nonzero(as_tuple=False).squeeze()

            all_loads[customer_idx] = new_load[customer_idx]
            all_demands[customer_idx, chosen_idx[customer_idx]] = new_demand[customer_idx].view(-1)
            all_demands[customer_idx, 0] = -1. + new_load[customer_idx].view(-1)

        if depot.any():
            all_loads[depot.nonzero(as_tuple=False).squeeze()] = 1.
            all_demands[depot.nonzero(as_tuple=False).squeeze(), 0] = 0.  # 只有去到仓库才会把这一点清零

        new_dynamic = torch.cat((all_loads.unsqueeze(1), all_demands.unsqueeze(1), all_SOC.unsqueeze(1),all_time.unsqueeze(1)),1).to(device)

        return torch.as_tensor(new_dynamic.data, device=dynamic.device), soc_consume



def render(static, tour_indices, save_path, dynamic, num_nodes, charging_num, batch_idx):
    """
    画出图形的解决方案
    """
    plt.close('all')
    plt.figure(figsize=(8,8))

    idx = tour_indices[0]                                                   #取出相应的batch的路由路线
    if len(idx.size()) == 1:
        idx = idx.unsqueeze(0)                                              #(1,sequence_len)
    idx = idx.expand(static.size(1), -1)                                    #(2,sequence_len)
    data = torch.gather(static[0].data, 1, idx).cpu().numpy()               #(2,equence_len)其中2为坐标点
    start = static[0, :, 0].cpu().data.numpy()                              #取出原点坐标
    point=static[0,:,:].cpu().numpy()
    demand = dynamic[0,1,:].float().cpu().numpy()*4
    x = np.hstack((start[0], data[0], start[0]))                            #np.hstack()将元素按水平方向进行叠加 x坐标：(1,sequence_len)
    y = np.hstack((start[1], data[1], start[1]))                            #y坐标：(1,squence_len)
    idx = np.hstack((0, tour_indices[0].cpu().numpy().flatten(), 0))        #将起点和终点以及路线图进行拼接：（1，squence_len+2)
    where = np.where(idx == 0)[0]                                           #找出哪些点的坐标为仓库
    where1 = np.where(idx == 1)[0]
    if where1:
        print(batch_idx)
        print(tour_indices)
    plt.rc('font', family='Times New Roman')

    for j in range(len(where) - 1):                                         #画几个往返
        low = where[j]                                                      #往返的起始索引
        high = where[j + 1]                                                 #往返的结束索引
        if low + 1 == high:                                                 #如果两个索引连在一起，则直接进行下一回合
            continue
        plt.plot(x[low: high + 1], y[low: high + 1], zorder=1, linewidth=2,label=f"Vehicle{j}")      #画图

    plt.scatter(point[0, charging_num + 1:], point[1, charging_num + 1:], s=10, c='b', zorder=2, label="Customer")  # 轴.scatter()画散点
    plt.scatter(point[0, 1:charging_num + 1], point[1, 1:charging_num + 1], s=40, c='green', marker='s', zorder=3, label="Station")  # 3个充电站画图
    plt.scatter(point[0, 0], point[1, 0], s=200, c='r', marker='*', zorder=3, label="Depot")  # 轴.scatter()画仓库点

    plt.legend(loc=2, fontsize=12, framealpha=0.2,bbox_to_anchor=(1.05, 1), borderaxespad=0)  # 轴.legend()增加图例
    plt.legend(fontsize=12, framealpha=1)  # 轴.legend()增加图例
    plt.xlim(-5, 105)
    plt.ylim(-5, 105)
    plt.title(f"C{num_nodes}-S{charging_num}",size=25, weight='bold')
    plt.yticks(fontproperties='Times New Roman', size=25)#设置大小及加粗
    plt.xticks(fontproperties='Times New Roman', size=25)
    ax = plt.gca()  # 获取边框
    bwith = 1.3
    ax.spines['bottom'].set_linewidth(bwith)
    ax.spines['left'].set_linewidth(bwith)
    ax.spines['top'].set_linewidth(bwith)
    ax.spines['right'].set_linewidth(bwith)

    # for i in range(charging_num + 1, num_nodes + charging_num + 1):
    #     plt.text((point[0, i]), point[1, i], f"{i}", size=12, color = "k",
    #             ha="center", va="top",weight = "bold")
    # plt.axis('off')
    # plt.show()
    plt.tight_layout()
    plt.savefig(save_path, bbox_inches='tight', dpi=200)