# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/102b_models.InceptionTimePlus.ipynb (unless otherwise specified).

__all__ = ['InceptionModulePlus', 'InceptionBlockPlus', 'InceptionTimePlus', 'InCoordTime', 'XCoordTime',
           'InceptionTimePlus17x17', 'InceptionTimePlus32x32', 'InceptionTimePlus47x47', 'InceptionTimePlus62x62',
           'InceptionTimeXLPlus', 'MultiInceptionTimePlus']

# Cell
from ..imports import *
from ..utils import *
from .layers import *
from .utils import *
torch.set_num_threads(cpus)

# Cell
# This is an unofficial PyTorch implementation by Ignacio Oguiza - oguiza@gmail.com modified from:

# Fawaz, H. I., Lucas, B., Forestier, G., Pelletier, C., Schmidt, D. F., Weber, J., ... & Petitjean, F. (2019).
# InceptionTime: Finding AlexNet for Time Series Classification. arXiv preprint arXiv:1909.04939.
# Official InceptionTime tensorflow implementation: https://github.com/hfawaz/InceptionTime


class InceptionModulePlus(Module):
    def __init__(self, ni, nf, ks=40, bottleneck=True, padding='same', coord=False, separable=False, dilation=1, stride=1, conv_dropout=0., sa=False, se=None,
                 norm='Batch', zero_norm=False, bn_1st=True, act=nn.ReLU, act_kwargs={}):
        if isinstance(ks, Integral): ks = [ks // (2**i) for i in range(3)]
        ks = [ksi if ksi % 2 != 0 else ksi - 1 for ksi in ks]  # ensure odd ks for padding='same'
        bottleneck = False if ni == nf else bottleneck
        self.bottleneck = Conv(ni, nf, 1, coord=coord, bias=False) if bottleneck else noop #
        self.convs = nn.ModuleList()
        for i in range(len(ks)): self.convs.append(Conv(nf if bottleneck else ni, nf, ks[i], padding=padding, coord=coord, separable=separable,
                                                         dilation=dilation**i, stride=stride, bias=False))
        self.mp_conv = nn.Sequential(*[nn.MaxPool1d(3, stride=1, padding=1), Conv(ni, nf, 1, coord=coord, bias=False)])
        self.concat = Concat()
        self.norm = Norm(nf * 4, norm=norm, zero_norm=zero_norm)
        self.conv_dropout = nn.Dropout(conv_dropout) if conv_dropout else noop
        self.sa = SimpleSelfAttention(nf * 4) if sa else noop
        self.act = act(**act_kwargs) if act else noop
        self.se = nn.Sequential(SqueezeExciteBlock(nf * 4, reduction=se), BN1d(nf * 4)) if se else noop

        self._init_cnn(self)

    def _init_cnn(self, m):
        if getattr(self, 'bias', None) is not None: nn.init.constant_(self.bias, 0)
        if isinstance(self, (nn.Conv1d,nn.Conv2d,nn.Conv3d,nn.Linear)): nn.init.kaiming_normal_(self.weight)
        for l in m.children(): self._init_cnn(l)

    def forward(self, x):
        input_tensor = x
        x = self.bottleneck(x)
        x = self.concat([l(x) for l in self.convs] + [self.mp_conv(input_tensor)])
        x = self.norm(x)
        x = self.conv_dropout(x)
        x = self.sa(x)
        x = self.act(x)
        x = self.se(x)
        return x


@delegates(InceptionModulePlus.__init__)
class InceptionBlockPlus(Module):
    def __init__(self, ni, nf, residual=True, depth=6, coord=False, norm='Batch', zero_norm=False, act=nn.ReLU, act_kwargs={}, sa=False, se=None,
                 keep_prob=1., **kwargs):
        self.residual, self.depth = residual, depth
        self.inception, self.shortcut, self.act = nn.ModuleList(), nn.ModuleList(), nn.ModuleList()
        for d in range(depth):
            self.inception.append(InceptionModulePlus(ni if d == 0 else nf * 4, nf, coord=coord, norm=norm,
                                                      zero_norm=zero_norm if d % 3 == 2 else False,
                                                      act=act if d % 3 != 2 else None, act_kwargs=act_kwargs,
                                                      sa=sa if d % 3 == 2 else False,
                                                      se=se if d % 3 != 2 else None,
                                                      **kwargs))
            if self.residual and d % 3 == 2:
                n_in, n_out = ni if d == 2 else nf * 4, nf * 4
                self.shortcut.append(Norm(n_in, norm=norm) if n_in == n_out else ConvBlock(n_in, n_out, 1, coord=coord, bias=False, norm=norm, act=None))
                self.act.append(act(**act_kwargs))
        self.add = Add()
        self.keep_prob = keep_prob

    def forward(self, x):
        res = x
        for i in range(self.depth):
            if self.keep_prob[i] > random.random() or not self.training:
                x = self.inception[i](x)
            if self.residual and i % 3 == 2:
                res = x = self.act[i//3](self.add(x, self.shortcut[i//3](res)))
        return x

# Cell
@delegates(InceptionModulePlus.__init__)
class InceptionTimePlus(nn.Sequential):
    def __init__(self, c_in, c_out, seq_len=None, nf=32, nb_filters=None, depth=6, stoch_depth=1.,
                 flatten=False, concat_pool=False, fc_dropout=0., bn=False, y_range=None, custom_head=None, **kwargs):

        if nb_filters is not None: nf = nb_filters
        else: nf = ifnone(nf, nb_filters) # for compatibility

        if stoch_depth != 0: keep_prob = np.linspace(1, stoch_depth, depth)
        else: keep_prob = np.array([1] * depth)
        backbone = InceptionBlockPlus(c_in, nf, depth=depth, keep_prob=keep_prob, **kwargs)

        #head
        self.head_nf = nf * 4
        self.c_out = c_out
        self.seq_len = seq_len
        if custom_head: head = custom_head(self.head_nf, c_out, seq_len)
        else: head = self.create_head(self.head_nf, c_out, seq_len, flatten=flatten, concat_pool=concat_pool,
                                      fc_dropout=fc_dropout, bn=bn, y_range=y_range)

        layers = OrderedDict([('backbone', nn.Sequential(backbone)), ('head', nn.Sequential(head))])
        super().__init__(layers)

    def create_head(self, nf, c_out, seq_len, flatten=False, concat_pool=False, fc_dropout=0., bn=False, y_range=None):
        if flatten:
            nf *= seq_len
            layers = [Flatten()]
        else:
            if concat_pool: nf *= 2
            layers = [GACP1d(1) if concat_pool else GAP1d(1)]
        layers += [LinBnDrop(nf, c_out, bn=bn, p=fc_dropout)]
        if y_range: layers += [SigmoidRange(*y_range)]
        return nn.Sequential(*layers)

# Cell
class InCoordTime(InceptionTimePlus):
    def __init__(self, *args, coord=True, zero_norm=True, **kwargs):
        super().__init__(*args, coord=coord, zero_norm=zero_norm, **kwargs)


class XCoordTime(InceptionTimePlus):
    def __init__(self, *args, coord=True, separable=True, zero_norm=True, **kwargs):
        super().__init__(*args, coord=coord, separable=separable, zero_norm=zero_norm, **kwargs)

InceptionTimePlus17x17 = partial(InceptionTimePlus, nf=17, depth=3)
setattr(InceptionTimePlus17x17, '__name__', 'InceptionTimePlus17x17')
InceptionTimePlus32x32 = InceptionTimePlus
InceptionTimePlus47x47 = partial(InceptionTimePlus, nf=47, depth=9)
setattr(InceptionTimePlus47x47, '__name__', 'InceptionTimePlus47x47')
InceptionTimePlus62x62 = partial(InceptionTimePlus, nf=62, depth=9)
setattr(InceptionTimePlus62x62, '__name__', 'InceptionTimePlus62x62')
InceptionTimeXLPlus = partial(InceptionTimePlus, nf=64, depth=12)
setattr(InceptionTimeXLPlus, '__name__', 'InceptionTimeXLPlus')

# Cell
@delegates(InceptionTimePlus.__init__)
class MultiInceptionTimePlus(nn.Sequential):
    """Class that allows you to create a model with multiple branches of InceptionTimePlus."""
    _arch = InceptionTimePlus
    def __init__(self, feat_list, c_out, seq_len=None, nf=32, nb_filters=None, depth=6, stoch_depth=1.,
                flatten=False, concat_pool=False, fc_dropout=0., bn=False, y_range=None, custom_head=None, device=None, **kwargs):
        """
        Args:
            feat_list: list with number of features that will be passed to each body.
        """
        self.feat_list = [feat_list] if isinstance(feat_list, int) else feat_list
        self.device = ifnone(device, default_device())

        # Backbone
        branches = nn.ModuleList()
        self.head_nf = 0
        for feat in self.feat_list:
            if is_listy(feat): feat = len(feat)
            m = build_ts_model(self._arch, c_in=feat, c_out=c_out, seq_len=seq_len, nf=nf, nb_filters=nb_filters,
                               depth=depth, stoch_depth=stoch_depth, **kwargs)
            with torch.no_grad():
                self.head_nf += m[0](torch.randn(1, feat, ifnone(seq_len, 10)).to(self.device)).shape[1]
            branches.append(m.backbone)
        backbone = _Splitter(self.feat_list, branches)

        # Head
        self.c_out = c_out
        self.seq_len = seq_len
        if custom_head is None:
            head = self._arch.create_head(self, self.head_nf, c_out, seq_len, flatten=flatten, concat_pool=concat_pool,
                                          fc_dropout=fc_dropout, bn=bn, y_range=y_range)
        else:
            head = custom_head(self.head_nf, c_out, seq_len)

        layers = OrderedDict([('backbone', nn.Sequential(backbone)), ('head', nn.Sequential(head))])
        super().__init__(layers)
        self.to(self.device)

# Internal Cell
class _Splitter(Module):
    def __init__(self, feat_list, branches):
        self.feat_list, self.branches = feat_list, branches
    def forward(self, x):
        if is_listy(self.feat_list[0]):
            x = [x[:, feat] for feat in self.feat_list]
        else:
            x = torch.split(x, self.feat_list, dim=1)
        _out = []
        for xi, branch in zip(x, self.branches): _out.append(branch(xi))
        output = torch.cat(_out, dim=1)
        return output