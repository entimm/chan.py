from typing import List, Union, overload

from data_process.bi.bi import CBi
from data_process.bi.bi_list import CBiList
from data_process.bsl_point.bs_point_list import CBSPointList
from data_process.chan_config import CChanConfig
from data_process.common.cenum import KLINE_DIR, SEG_TYPE
from data_process.common.chan_exception import CChanException, ErrCode
from data_process.seg.seg import CSeg
from data_process.seg.seg_config import CSegConfig
from data_process.seg.seg_list_comm import CSegListComm
from data_process.zs.zs_list import CZSList

from .kline import CKLine
from .kline_unit import CKLine_Unit


def get_seglist_instance(seg_config: CSegConfig, lv) -> CSegListComm:
    """根据配置返回相应的线段列表实例"""
    if seg_config.seg_algo == "chan":
        from data_process.seg.seg_list_chan import CSegListChan
        return CSegListChan(seg_config, lv)
    elif seg_config.seg_algo == "1+1":
        print(f'Please avoid using seg_algo={seg_config.seg_algo} as it is deprecated and no longer maintained.')
        from data_process.seg.seg_list_dyh import CSegListDYH
        return CSegListDYH(seg_config, lv)
    elif seg_config.seg_algo == "break":
        print(f'Please avoid using seg_algo={seg_config.seg_algo} as it is deprecated and no longer maintained.')
        from data_process.seg.seg_list_def import CSegListDef
        return CSegListDef(seg_config, lv)
    else:
        raise CChanException(f"unsupport seg algoright:{seg_config.seg_algo}", ErrCode.PARA_ERROR)


class CKLine_List:
    def __init__(self, kl_type, conf: CChanConfig):
        self.kl_type = kl_type
        self.config = conf
        self.lst: List[CKLine] = []  # K线列表，可递归  元素KLine类型
        self.bi_list = CBiList(bi_conf=conf.bi_conf)
        self.seg_list: CSegListComm[CBi] = get_seglist_instance(seg_config=conf.seg_conf, lv=SEG_TYPE.BI)
        self.segseg_list: CSegListComm[CSeg[CBi]] = get_seglist_instance(seg_config=conf.seg_conf, lv=SEG_TYPE.SEG)

        self.zs_list = CZSList(zs_config=conf.zs_conf)
        self.segzs_list = CZSList(zs_config=conf.zs_conf)

        self.bs_point_lst = CBSPointList[CBi, CBiList](bs_point_config=conf.bs_point_conf)
        self.seg_bs_point_lst = CBSPointList[CSeg, CSegListComm](bs_point_config=conf.seg_bs_point_conf)

        self.metric_model_lst = conf.GetMetricModel()

        self.step_calculation = self.need_cal_step_by_step()

    @overload
    def __getitem__(self, index: int) -> CKLine: ...

    @overload
    def __getitem__(self, index: slice) -> List[CKLine]: ...

    def __getitem__(self, index: Union[slice, int]) -> Union[List[CKLine], CKLine]:
        return self.lst[index]

    def __len__(self):
        return len(self.lst)

    def cal_seg_and_zs(self):
        """计算线段和中枢"""
        if not self.step_calculation:
            self.bi_list.try_add_virtual_bi(self.lst[-1])
        cal_seg(self.bi_list, self.seg_list)
        self.zs_list.cal_bi_zs(self.bi_list, self.seg_list)
        update_zs_in_seg(self.bi_list, self.seg_list, self.zs_list)  # 计算seg的zs_lst，以及中枢的bi_in, bi_out

        cal_seg(self.seg_list, self.segseg_list)
        self.segzs_list.cal_bi_zs(self.seg_list, self.segseg_list)
        update_zs_in_seg(self.seg_list, self.segseg_list, self.segzs_list)  # 计算segseg的zs_lst，以及中枢的bi_in, bi_out

        self.update_klc_in_bi()  # 计算每一笔里面的 klc列表

        # 计算买卖点
        self.seg_bs_point_lst.cal(self.seg_list, self.segseg_list)  # 线段线段买卖点
        self.bs_point_lst.cal(self.bi_list, self.seg_list)  # 再算笔买卖点

    def need_cal_step_by_step(self):
        """判断是否需要逐步计算"""
        return self.config.triger_step

    def add_single_klu(self, klu: CKLine_Unit):
        """添加单个K线单位到K线列表"""
        klu.set_metric(self.metric_model_lst)
        if len(self.lst) == 0:
            self.lst.append(CKLine(klu, idx=0))
        else:
            _dir = self.lst[-1].try_add(klu)
            if _dir != KLINE_DIR.COMBINE:  # 不需要合并K线
                self.lst.append(CKLine(klu, idx=len(self.lst), _dir=_dir))
                if len(self.lst) >= 3:
                    self.lst[-2].update_fx(self.lst[-3], self.lst[-1])
                if self.bi_list.update_bi(self.lst[-2], self.lst[-1], self.step_calculation) and self.step_calculation:
                    self.cal_seg_and_zs()
            elif self.step_calculation and self.bi_list.try_add_virtual_bi(self.lst[-1], need_del_end=True):  # 这里的必要性参见issue#175
                self.cal_seg_and_zs()

    def klu_iter(self, klc_begin_idx=0):
        """迭代K线单位"""
        for klc in self.lst[klc_begin_idx:]:
            yield from klc.lst

    def update_klc_in_bi(self):
        """更新每一笔中的K线列表"""
        for bi in self.bi_list:
            bi.set_klc_lst(self[bi.begin_klc.idx:bi.end_klc.idx+1])


def cal_seg(bi_list, seg_list):
    """计算线段"""
    seg_list.update(bi_list)
    # 计算每一笔属于哪个线段
    bi_seg_idx_dict = {}
    for seg_idx, seg in enumerate(seg_list):
        for i in range(seg.begin_bi.idx, seg.end_bi.idx+1):
            bi_seg_idx_dict[i] = seg_idx
    for bi in bi_list:
        bi.set_seg_idx(bi_seg_idx_dict.get(bi.idx, len(seg_list)))  # 找不到的应该都是最后一个线段的


def update_zs_in_seg(bi_list, seg_list, zs_list):
    """更新线段中的中枢"""
    for seg in seg_list:
        seg.clear_zs_lst()
        for zs in zs_list:
            if zs.is_inside(seg):
                seg.add_zs(zs)
            assert zs.begin_bi.idx > 0
            zs.set_bi_in(bi_list[zs.begin_bi.idx-1])
            if zs.end_bi.idx+1 < len(bi_list):
                zs.set_bi_out(bi_list[zs.end_bi.idx+1])
            zs.set_bi_lst(list(bi_list[zs.begin_bi.idx:zs.end_bi.idx+1]))
